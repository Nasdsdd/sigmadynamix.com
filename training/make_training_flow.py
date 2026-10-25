#!/usr/bin/env python3
"""
make_training_flow.py -- volumes Rithmic (SigmaTradeHistory)  ->  flow_AAAAMMJJ.json du training

Pour chaque daypack_AAAAMMJJ.json du dossier daypacks/, lit dans le dossier History de
SigmaTradeHistory :

    profile1m_AAAAMMJJ.csv   horodatage_ms;symbole;prix;achats;ventes;sans_cote   (obligatoire)
    trades_AAAAMMJJ.csv      horodatage_ms;symbole;prix;taille;agresseur          (facultatif)

et ecrit a cote du daypack :

    daypacks/flow_AAAAMMJJ.json

Contenu (rien n'est recalcule par la page, sauf l'agregation progressive) :
    rth {t0, t1}           seance 09:30-16:00 New York (ms UTC)
    m0, mb, ms, mn         minutes de la seance : achats / ventes / sans cote par minute
                           (index i = minute m0 + i x 60 s) -> delta et CVD (remis a 0 a 09:30)
    base, pc, pp, pb, ps, pn   profil par minute : pc[i] lignes pour la minute i ; prix en ticks
                           (pp = tick - base), achats, ventes, sans cote -> volume profile de la
                           seance construit minute par minute
    prev {day, poc, vah, val, base, pp, pv}   seance precedente : niveaux (70 %, lignes d'un tick,
                           regle du Terminal) et son profil agrege
    bo {params, side, list}   gros ordres [t, prix, taille, cote(+1/-1), etat(1 absorbe, 2 pousse),
                           avance en points] -- seulement si trades_ existe. Regle du Terminal :
                           prints de meme ms et meme cote = un ordre ; gros si taille > max(minLots,
                           moyenne + k x ecart type des 3 000 derniers ordres) ; absorbe si le prix
                           n'avance pas de plus de `ticks` ticks dans son sens en `seconds` s.
                           Cote : agresseur de Rithmic s'il est la ; sinon regle du tick, BORNEE par
                           les achats / ventes de Rithmic a ce prix dans cette minute (les totaux
                           par prix restent exacts).

Usage :
    python3 make_training_flow.py C:\\SigmaData\\History [-d daypacks] [--force]
"""

import argparse
import datetime
import glob
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
FORMAT = "sigma-flow-v1"
BO = {"sigma": 2.0, "minLots": 10, "seconds": 30, "ticks": 2}
BO_WINDOW, BO_MIN_SAMPLES = 3000, 200


def ny_ms(day, hh, mm):
    return int(datetime.datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY).timestamp() * 1000)


def read_profile(path, tick):
    """{minute_ms: {tick: [b, s, n]}}"""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            f = line.strip().split(";")
            if len(f) < 6:
                continue
            m = int(f[0])
            k = int(round(float(f[2]) / tick))
            row = out.setdefault(m, {}).setdefault(k, [0, 0, 0])
            row[0] += int(f[3]); row[1] += int(f[4]); row[2] += int(f[5])
    return out


def value_area(vol_by_tick, pct=70.0):
    """POC / VAH / VAL en ticks, grille d'un tick, regle du VolumeProfileStore du Terminal."""
    if not vol_by_tick:
        return None
    lo, hi = min(vol_by_tick), max(vol_by_tick)
    n = hi - lo + 1
    tot = [vol_by_tick.get(lo + i, 0) for i in range(n)]
    total = sum(tot)
    mid = (n - 1) / 2.0
    poc = 0
    for i in range(1, n):
        if tot[i] > tot[poc] or (tot[i] == tot[poc] and abs(i - mid) < abs(poc - mid)):
            poc = i
    target = math.ceil(total * pct / 100.0)
    vah = val = poc
    acc = tot[poc]
    while acc < target and (vah < n - 1 or val > 0):
        up = tot[vah + 1] if vah < n - 1 else -1
        down = tot[val - 1] if val > 0 else -1
        if up >= down:
            vah += 1; acc += up
        else:
            val -= 1; acc += down
    return lo + poc, lo + vah, lo + val


def session_profile(prof, t0, t1):
    agg = {}
    for m, rows in prof.items():
        if t0 <= m < t1:
            for k, (b, s, n) in rows.items():
                agg[k] = agg.get(k, 0) + b + s + n
    return agg


def big_orders(trades_path, prof, tick, keep_from):
    """Rend (liste, mode de cote). Reprend BigOrderTracker du Terminal (v2.5.1.6)."""
    prints = []
    with open(trades_path, encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split(";")
            if len(f) < 5:
                continue
            prints.append((int(f[0]), float(f[2]), int(f[3]), f[4].strip()))
    has_aggr = any(p[3] in ("B", "S") for p in prints)
    budget = {}
    for m, rows in prof.items():
        for k, (b, s, n) in rows.items():
            budget[(m, k)] = [b, s]

    last_px, last_side = None, 1
    sides = []
    for ms, px, size, ag in prints:
        if ag in ("B", "S"):
            side = 1 if ag == "B" else -1
        else:
            side = last_side if last_px is None or px == last_px else (1 if px > last_px else -1)
            bud = budget.get((ms // 60000 * 60000, int(round(px / tick))))
            if bud is not None:
                i = 0 if side > 0 else 1
                if bud[i] < size and bud[1 - i] >= size:
                    side = -side
                    i = 1 - i
                bud[i] = max(0, bud[i] - size)
        sides.append(side)
        last_px, last_side = px, side

    # Regroupement meme ms + meme cote, puis statistique et fenetre d'absorption.
    orders = []
    cur = None
    for (ms, px, size, _), side in zip(prints, sides):
        if cur and cur[0] == ms and cur[3] == side:
            cur[2] += size; cur[1] = px
        else:
            if cur:
                orders.append(tuple(cur))
            cur = [ms, px, size, side]
    if cur:
        orders.append(tuple(cur))

    sizes = [0] * BO_WINDOW
    count = nxt = 0
    s1 = s2 = 0.0
    found = []
    for ms, px, size, side in orders:
        if count >= BO_MIN_SAMPLES:
            mean = s1 / count
            sd = math.sqrt(max(0.0, s2 / count - mean * mean))
            if size >= max(BO["minLots"], mean + BO["sigma"] * sd):   # meme regle que le Terminal (>=)
                found.append([ms, px, size, side])
        if count == BO_WINDOW:
            old = sizes[nxt]; s1 -= old; s2 -= old * old
        else:
            count += 1
        sizes[nxt] = size; s1 += size; s2 += size * size
        nxt = (nxt + 1) % BO_WINDOW

    # Absorption : extreme atteint dans le sens de l'ordre pendant `seconds` secondes.
    out = []
    j = 0
    win = BO["seconds"] * 1000
    for ms, px, size, side in found:
        if ms < keep_from:
            continue
        while j < len(prints) and prints[j][0] <= ms:
            j += 1
        ext = px
        k = j
        while k < len(prints) and prints[k][0] <= ms + win:
            ext = max(ext, prints[k][1]) if side > 0 else min(ext, prints[k][1])
            k += 1
        move = (ext - px) * side
        state = 2 if move > BO["ticks"] * tick + 1e-9 else 1
        out.append([ms, px, size, side, state, round(move, 2)])
    return out, ("aggressor" if has_aggr else "tick-rule bounded by profile")


def build(day, hist, daypack, prev_day):
    tick = float(daypack.get("tickSize", 0.25))
    ymd = day.strftime("%Y%m%d")
    prof = read_profile(os.path.join(hist, f"profile1m_{ymd}.csv"), tick)
    t0, t1 = ny_ms(day, 9, 30), ny_ms(day, 16, 0)
    nmin = (t1 - t0) // 60000
    mb, msl, mn, pc, pp, pb, ps, pn = ([] for _ in range(8))
    ticks_all = [k for m, rows in prof.items() if t0 <= m < t1 for k in rows]
    if not ticks_all:
        raise ValueError(f"{day} : aucune minute de seance dans profile1m_{ymd}.csv")
    base = min(ticks_all)
    for i in range(nmin):
        rows = prof.get(t0 + i * 60000, {})
        mb.append(sum(r[0] for r in rows.values()))
        msl.append(sum(r[1] for r in rows.values()))
        mn.append(sum(r[2] for r in rows.values()))
        pc.append(len(rows))
        for k in sorted(rows):
            b, s, n = rows[k]
            pp.append(k - base); pb.append(b); ps.append(s); pn.append(n)

    out = {"format": FORMAT, "day": day.isoformat(), "symbol": daypack.get("symbol"), "tickSize": tick,
           "rth": {"t0": t0, "t1": t1}, "m0": t0, "mb": mb, "ms": msl, "mn": mn,
           "base": base, "pc": pc, "pp": pp, "pb": pb, "ps": ps, "pn": pn}
    va = value_area(session_profile(prof, t0, t1))
    summary = {"day": day.isoformat(), "vol": sum(mb) + sum(msl) + sum(mn), "delta": sum(mb) - sum(msl),
               "poc": va[0] * tick if va else None, "prev": None, "bo": None}

    if prev_day:
        pprof = read_profile(os.path.join(hist, f"profile1m_{prev_day.strftime('%Y%m%d')}.csv"), tick)
        agg = session_profile(pprof, ny_ms(prev_day, 9, 30), ny_ms(prev_day, 16, 0))
        pva = value_area(agg)
        if pva:
            pbase = min(agg)
            ks = sorted(agg)
            out["prev"] = {"day": prev_day.isoformat(), "poc": pva[0] * tick, "vah": pva[1] * tick, "val": pva[2] * tick,
                           "base": pbase, "pp": [k - pbase for k in ks], "pv": [agg[k] for k in ks]}
            summary["prev"] = prev_day.isoformat()

    tpath = os.path.join(hist, f"trades_{ymd}.csv")
    if os.path.exists(tpath) and os.path.getsize(tpath) > 0:
        lst, mode = big_orders(tpath, prof, tick, keep_from=int(daypack.get("t0", t0)))
        out["bo"] = {"params": BO, "side": mode, "list": lst}
        summary["bo"] = len(lst)
    return out, summary


def main():
    ap = argparse.ArgumentParser(description="Ajoute les volumes Rithmic aux journees du training.")
    ap.add_argument("history", help="dossier History de SigmaTradeHistory (profile1m_*.csv, trades_*.csv)")
    ap.add_argument("-d", "--daypacks", default="daypacks", help="dossier des daypacks (defaut : daypacks)")
    ap.add_argument("--force", action="store_true", help="refaire les flow_ deja presents")
    a = ap.parse_args()

    have = sorted(datetime.date(int(p[-12:-8]), int(p[-8:-6]), int(p[-6:-4]))
                  for p in glob.glob(os.path.join(a.history, "profile1m_*.csv")))
    packs = sorted(glob.glob(os.path.join(a.daypacks, "daypack_*.json")))
    print(f"{'JOURNEE':<12}{'VOLUME':>10}{'DELTA':>9}{'POC':>10}  {'VEILLE':<11}{'GROS ORDRES':>12}")
    written = skipped = missing = 0
    for p in packs:
        ymd = os.path.basename(p)[8:16]
        day = datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        dest = os.path.join(a.daypacks, f"flow_{ymd}.json")
        if day not in have:
            missing += 1
            continue
        if os.path.exists(dest) and not a.force:
            skipped += 1
            continue
        with open(p, encoding="utf-8") as fh:
            dp = json.load(fh)
        prev = next((d for d in reversed(have) if d < day and d.weekday() < 5), None)
        try:
            out, s = build(day, a.history, dp, prev)
        except ValueError as err:
            print(f"REFUSE     {err}", file=sys.stderr)
            continue
        with open(dest + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(out, fh, separators=(",", ":"))
        os.replace(dest + ".tmp", dest)
        written += 1
        print(f"{s['day']:<12}{s['vol']:>10,}{s['delta']:>+9,}{s['poc'] or 0:>10.2f}  {s['prev'] or '-':<11}"
              f"{('-' if s['bo'] is None else s['bo']):>12}")
    print(f"\n{written} flow_ ecrit(s), {skipped} deja present(s) (--force pour refaire), "
          f"{missing} journee(s) sans profile1m_ dans {a.history}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
