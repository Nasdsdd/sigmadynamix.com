#!/usr/bin/env python3
"""
make_training_daypacks.py — exports de l'outil des niveaux  ->  daypacks du training

Lit un ou plusieurs exports au format `sigma-levels-v1` (ceux que produit
sigma_levels_builder.html) et ecrit un fichier par journee :

    daypacks/daypack_AAAAMMJJ.json

Chaque fichier contient tout ce dont la page de training a besoin pour rejouer
la journee, sans aucun recalcul :

    symbol, tickSize, enc:"delta1", t0, p0, dts, dps   le pack de ticks
    levels                                             les niveaux cles
    orb {hi, lo} et orbAt                              la plage d'ouverture et son heure
    window {t0, t1}, gapMs                             la fenetre de signaux et l'ecart minimum
    signals                                            la table figee des signaux

Usage :
    python3 make_training_daypacks.py export1.json [export2.json ...] [-o daypacks]

Une journee sans signal est refusee. Une date presente dans deux exports est
signalee et la journee n'est pas ecrite deux fois.
"""

import argparse
import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ORB_END_NY = (9, 45)          # la plage d'ouverture se ferme a 09:45 New York
FORMAT = "sigma-levels-v1"


def ny_ms(day_iso, hh, mm):
    """Horodatage UTC en ms du jour `day_iso` a hh:mm heure de New York."""
    y, m, d = (int(x) for x in day_iso.split("-"))
    return int(datetime.datetime(y, m, d, hh, mm, tzinfo=NY).timestamp() * 1000)


def load_export(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("format") != FORMAT:
        raise SystemExit(f"{os.path.basename(path)} : format inattendu "
                         f"({doc.get('format')!r}, attendu {FORMAT!r})")
    if not isinstance(doc.get("days"), list) or not doc["days"]:
        raise SystemExit(f"{os.path.basename(path)} : aucune journee dans l'export")
    return doc


def build_day(day, params, source):
    """Construit le daypack d'une journee. Renvoie (nom de fichier, contenu, resume)."""
    iso = day["day"]
    pack = day["pack"]
    signals = day.get("signals") or []
    levels = day.get("levels") or []

    if not signals:
        raise ValueError(f"{iso} : aucun signal, journee refusee")
    for champ in ("tickSize", "t0", "p0", "dts", "dps"):
        if champ not in pack:
            raise ValueError(f"{iso} : le pack ne contient pas {champ!r}")
    if len(pack["dts"]) != len(pack["dps"]):
        raise ValueError(f"{iso} : dts et dps n'ont pas la meme longueur")

    window = day.get("window") or {}
    if "t0" not in window or "t1" not in window:
        raise ValueError(f"{iso} : fenetre de signaux absente")

    # Heure de mise en service d'un niveau pose en cours de seance. Un export
    # ancien n'en a pas : le niveau vit alors des l'ouverture de la fenetre.
    debut = str(params.get("start", "09:45"))
    for l in levels:
        l.setdefault("from", debut)
        if "fromMs" not in l:
            hh, mm = (int(x) for x in str(l["from"]).split(":")[:2])
            l["fromMs"] = max(window["t0"], ny_ms(iso, hh, mm))

    gap_ms = int(round(float(params.get("gapMin", 3)) * 60000))
    orb = day.get("orb") or None

    out = {
        "day": iso,
        "symbol": day.get("symbol") or "NQ",
        "tickSize": pack["tickSize"],
        "enc": "delta1",
        "t0": pack["t0"],
        "p0": pack["p0"],
        "dts": pack["dts"],
        "dps": pack["dps"],
        "levels": levels,
        "orb": orb,
        "orbAt": ny_ms(iso, *ORB_END_NY),
        "window": {"t0": window["t0"], "t1": window["t1"]},
        "gapMs": gap_ms,
        "signals": signals,
        "source": os.path.basename(source),
    }

    # « dont ORB » : les niveaux poses sur un bord de la plage d'ouverture, a
    # moins d'une demi-zone de celui-ci. Les deux bords eux-memes sont servis a
    # part (champ orb) et affiches par la page dans Partner Trading.
    def sur_orb(l):
        if not orb:
            return False
        z = float(l.get("zone", 0))
        return any(abs(float(l["price"]) - orb[b]) <= z for b in ("hi", "lo") if orb.get(b) is not None)

    n_orb = sum(1 for l in levels if sur_orb(l))
    resume = {
        "day": iso,
        "levels": len(levels),
        "orb": n_orb,
        "signals": len(signals),
        "ticks": len(pack["dts"]) + 1,
    }
    return f"daypack_{iso.replace('-', '')}.json", out, resume


def main():
    ap = argparse.ArgumentParser(description="Construit les daypacks du training "
                                             "depuis les exports de l'outil des niveaux.")
    ap.add_argument("exports", nargs="+", help="fichiers JSON au format sigma-levels-v1")
    ap.add_argument("-o", "--out", default="daypacks", help="dossier de sortie (defaut : daypacks)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="ne pas toucher aux journees deja presentes dans le dossier de sortie")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    vus = {}          # date -> export qui l'a fournie en premier
    resumes = []
    doublons = []
    refus = []
    ignores = []      # journees deja presentes, laissees telles quelles

    for chemin in args.exports:
        doc = load_export(chemin)
        params = doc.get("params") or {}
        for day in doc["days"]:
            iso = day.get("day")
            if iso in vus:
                doublons.append((iso, vus[iso], os.path.basename(chemin)))
                continue
            if args.skip_existing and os.path.exists(
                    os.path.join(args.out, f"daypack_{str(iso).replace('-', '')}.json")):
                ignores.append(iso)
                vus[iso] = os.path.basename(chemin)
                continue
            try:
                nom, contenu, resume = build_day(day, params, chemin)
            except ValueError as err:
                refus.append(str(err))
                continue
            with open(os.path.join(args.out, nom), "w", encoding="utf-8") as fh:
                json.dump(contenu, fh, separators=(",", ":"))
            resume["file"] = nom
            resume["bytes"] = os.path.getsize(os.path.join(args.out, nom))
            resumes.append(resume)
            vus[iso] = os.path.basename(chemin)

    resumes.sort(key=lambda r: r["day"])
    print(f"{'JOURNEE':<12}{'NIVEAUX':>9}{'DONT ORB':>10}{'SIGNAUX':>9}{'TICKS':>9}{'TAILLE':>10}   FICHIER")
    for r in resumes:
        print(f"{r['day']:<12}{r['levels']:>9}{r['orb']:>10}{r['signals']:>9}"
              f"{r['ticks']:>9}{r['bytes']/1e6:>9.1f}M   {r['file']}")
    print(f"\n{len(resumes)} journee(s) ecrite(s) dans {args.out}/")
    if ignores:
        print(f"{len(ignores)} deja presente(s), laissee(s) telle(s) quelle(s) : " + ", ".join(sorted(ignores)))

    for iso, a, b in doublons:
        print(f"ATTENTION  {iso} : deja fournie par {a}, la copie de {b} est ignoree", file=sys.stderr)
    for msg in refus:
        print(f"REFUSE     {msg}", file=sys.stderr)
    if refus:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
