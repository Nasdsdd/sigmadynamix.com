#!/usr/bin/env python3
"""
Sigma Dynamix — operator portal builder.

Reads the trade CSVs dropped in incoming/, the roster in config/roster.json and
the payments in config/payouts.csv, then writes one AES-GCM encrypted payload
per operator into data/.

Each payload can only be opened with that operator's own password. The password
never leaves your machine and is never published: only the encrypted blob and a
salt are committed.

    python3 tools/build_portal.py

Options:
    --root DIR      project root (default: the parent of tools/)
    --quiet         only print warnings and errors
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run:  pip install cryptography")

# --- must match the values in login.html -----------------------------------
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32

# --- contract economics ----------------------------------------------------
POINT_VALUE = 20.00       # NQ: USD per point
ROUND_TURN_FEE = 5.16     # USD per contract, per round turn

# Two export layouts are accepted.
#
#   legacy — one broker account per operator leg; the Account column IS the handle
#   desk   — every reference trade sits on one broker account (SDGS2) and the
#            Operator column carries the handle, alongside Role / Direct / LegId
#
# The builder detects which one it is reading and normalises both to the same
# shape, so you can mix old and new files in incoming/ without thinking about it.
CORE_COLUMNS = [
    "Contract", "Side", "Quantity",
    "OpenPrice", "ClosePrice", "OpenDate", "OpenTime", "CloseTime",
]
LEGACY_KEY = "Account"
DESK_KEY = "Operator"

TEST_ACCOUNT = re.compile(r"^SDTest", re.I)


# ===========================================================================
# helpers
# ===========================================================================
def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def email_id(email: str) -> str:
    """Stable, non-reversible handle for an email address."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               PBKDF2_ITERATIONS, dklen=KEY_BYTES)


def salt_for(email: str) -> bytes:
    """A stable salt, one per operator.

    A random salt per build would change the derived key every time, and the
    dashboard keeps that key so it can refresh itself without asking the
    operator to sign in again. Deriving the salt from the email keeps it unique
    per operator — which is all a salt is for — while staying the same from one
    publication to the next. The nonce stays random per build, so no two
    encryptions ever share one.
    """
    return hashlib.sha256(
        b"sigma-dynamix|portal|v1|" + email.strip().lower().encode("utf-8")
    ).digest()[:SALT_BYTES]


def encrypt(payload: dict, password: str, email: str) -> dict:
    salt = salt_for(email)
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    blob = AESGCM(derive_key(password, salt)).encrypt(nonce, plaintext, None)
    return {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": b64(salt),
        "nonce": b64(nonce),
        "data": b64(blob),
    }


def last_four(value: str) -> str:
    """Keep only the last four characters of an account reference.

    Whatever you put in `bankHint` — a full IBAN, a formatted one, or just the
    last digits — only these four ever reach the published payload. The
    dashboard renders them as "XXXX 4417".
    """
    digits = re.sub(r"[^0-9A-Za-z]", "", value or "")
    return digits[-4:] if len(digits) >= 4 else ""


def fail(msg: str) -> None:
    sys.exit(f"\n  ERROR  {msg}\n")


# ===========================================================================
# loading
# ===========================================================================
def load_roster(path: Path) -> list[dict]:
    if not path.exists():
        fail(f"No roster at {path}. Copy config/roster.example.json and fill it in.")
    try:
        roster = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{path.name} is not valid JSON — {e}")

    if not isinstance(roster, list) or not roster:
        fail(f"{path.name} must be a non-empty list of operators.")

    seen_email, seen_acct = {}, {}
    for i, op in enumerate(roster):
        for field in ("name", "email", "password", "accounts"):
            if not op.get(field):
                fail(f"Operator #{i + 1} in {path.name} is missing '{field}'.")
        if len(op["password"]) < 10:
            fail(f"Password for {op['email']} is too short — use at least 10 characters. "
                 "Generate one with:  python3 tools/build_portal.py --password")
        key = op["email"].strip().lower()
        if key in seen_email:
            fail(f"Email {key} appears twice in {path.name}.")
        seen_email[key] = op["name"]
        for a in op["accounts"]:
            if a in seen_acct:
                fail(f"Account {a} is assigned to both {seen_acct[a]} and {op['name']}.")
            seen_acct[a] = op["name"]
    return roster


def load_trades(incoming: Path, quiet: bool) -> tuple[list[dict], int]:
    """Returns (normalised rows, follower rows skipped).

    Every returned row carries a `handle` key — the SDOpe identity the roster
    matches on — whichever layout it came from.
    """
    files = sorted(incoming.glob("*.csv"))
    if not files:
        # Not an error: a desk with no published session yet still needs its
        # operators to have accounts and an (empty) dashboard.
        if not quiet:
            print(f"    no CSV in {incoming.name}/ — dashboards will be empty")
        return [], 0

    rows, seen, followers = [], set(), 0

    for f in files:
        with f.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            head = reader.fieldnames or []

            missing = [c for c in CORE_COLUMNS if c not in head]
            if missing:
                fail(f"{f.name} is missing column(s): {', '.join(missing)}")

            if DESK_KEY in head:
                layout, key = "desk", DESK_KEY
            elif LEGACY_KEY in head:
                layout, key = "legacy", LEGACY_KEY
            else:
                fail(f"{f.name} has neither an {DESK_KEY} nor an {LEGACY_KEY} column — "
                     "the builder cannot tell whose trades these are.")

            n, skipped = 0, 0
            for r in reader:
                r = {k: (v or "").strip() for k, v in r.items() if k}

                # Follower rows are the firm's copy on a prop account, not the
                # operator's own execution. Counting them would double the P&L.
                if r.get("Role", "").lower() == "follower":
                    skipped += 1
                    continue

                handle = r.get(key, "")
                if not handle:
                    continue
                r["handle"] = handle
                r.setdefault("Account", handle)
                r.setdefault("CloseDate", r.get("OpenDate", ""))

                fp = (handle, r["OpenDate"], r["OpenTime"],
                      r["CloseTime"], r["OpenPrice"], r["ClosePrice"], r["Side"])
                if fp in seen:
                    continue
                seen.add(fp)
                rows.append(r)
                n += 1

            followers += skipped

        if not quiet:
            note = f"{layout:<6}"
            extra = f"  ({skipped} follower rows skipped)" if skipped else ""
            print(f"    {f.name:<44} {note} {n:>5} trades{extra}")

    return rows, followers


def load_payouts(path: Path) -> dict[str, list[dict]]:
    """config/payouts.csv → {operator email: [payments]}"""
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            r = {k: (v or "").strip() for k, v in r.items() if k}
            if not r.get("Email"):
                continue

            def num(field):
                v = r.get(field, "").replace(",", ".").replace(" ", "")
                try:
                    return float(v)
                except ValueError:
                    return None

            out.setdefault(r["Email"].lower(), []).append({
                "period": r.get("Period", ""),
                "basis": num("TradingNet"),
                "share": num("Share"),
                "paid": num("Paid"),
                "floor": (r.get("MinimumApplied", "").lower() in ("yes", "y", "true", "1", "oui")),
                "value": r.get("ValueDate", ""),
                "method": r.get("Method", "SEPA transfer"),
                "ref": r.get("Reference", "—"),
                "status": (r.get("Status", "sent").lower() or "sent"),
            })
    for v in out.values():
        v.sort(key=lambda p: p["value"] or "")
    return out


# ===========================================================================
# trade maths
# ===========================================================================
def normalise(r: dict) -> dict | None:
    try:
        qty = int(float(r["Quantity"]))
        op, cp = float(r["OpenPrice"]), float(r["ClosePrice"])
    except (ValueError, KeyError):
        return None
    side = r["Side"].upper()[:1]
    if side not in ("B", "S"):
        return None
    pts = ((cp - op) if side == "B" else (op - cp)) * qty
    return {
        "account": r.get("handle") or r["Account"],
        "brokerAccount": r.get("Account", ""),
        "contract": r.get("Contract", "NQ"),
        "side": side,
        "qty": qty,
        "open": round(op, 2),
        "close": round(cp, 2),
        "openDate": r["OpenDate"],
        "openTime": r["OpenTime"],
        "closeDate": r.get("CloseDate", r["OpenDate"]),
        "closeTime": r["CloseTime"],
        "pts": round(pts, 4),
        "net": round(pts * POINT_VALUE - ROUND_TURN_FEE * qty, 4),
    }


# ===========================================================================
# build
# ===========================================================================
def build(root: Path, quiet: bool) -> None:
    cfg, incoming, data = root / "config", root / "incoming", root / "data"
    data.mkdir(parents=True, exist_ok=True)

    say = (lambda *a: None) if quiet else print

    say("\n  Sigma Dynamix — portal build\n")
    say("  Roster")
    roster = load_roster(cfg / "roster.json")
    say(f"    {len(roster)} operator(s)\n")

    say("  Trade files")
    raw, followers = load_trades(incoming, quiet)
    say(f"    {'':<44} {'':<6} {len(raw):>5} unique\n")

    payouts = load_payouts(cfg / "payouts.csv")

    owner = {a: op for op in roster for a in op["accounts"]}
    buckets: dict[str, list[dict]] = {op["email"].lower(): [] for op in roster}
    skipped_test, unmapped, malformed = 0, {}, 0
    before_start: dict[str, int] = {}

    for r in raw:
        acct = r["handle"]
        if TEST_ACCOUNT.match(acct):
            skipped_test += 1
            continue
        op = owner.get(acct)
        if not op:
            unmapped[acct] = unmapped.get(acct, 0) + 1
            continue
        t = normalise(r)
        if t is None:
            malformed += 1
            continue

        # An operator's dashboard begins on their start date. Anything earlier
        # — an evaluation period on the same accounts — stays unpublished. The
        # CSVs keep it; only the dashboard ignores it.
        start = (op.get("startedOn") or "").strip()
        if start and t["openDate"] < start:
            before_start[op["name"]] = before_start.get(op["name"], 0) + 1
            continue

        buckets[op["email"].lower()].append(t)

    for v in buckets.values():
        v.sort(key=lambda t: (t["openDate"], t["openTime"], t["account"]))

    # ---- write one encrypted payload per operator -------------------------
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    directory, written = {}, []

    for op in roster:
        email = op["email"].lower()
        trades = buckets[email]
        payload = {
            "name": op["name"],
            "email": op["email"],
            "accounts": op["accounts"],
            "startedOn": op.get("startedOn", ""),
            "minimum": op.get("minimum", ""),
            "bankHint": last_four(op.get("bankHint", "")),
            "pointValue": POINT_VALUE,
            "roundTurnFee": ROUND_TURN_FEE,
            "generatedAt": stamp,
            "trades": trades,
            "payouts": payouts.get(email, []),
        }
        handle = email_id(email)
        envelope = encrypt(payload, op["password"], email)
        name = f"op_{handle[:24]}.json"
        (data / name).write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")

        directory[handle] = {"file": name}
        written.append(name)

        days = len({t["openDate"] for t in trades})
        net = sum(t["net"] for t in trades)
        say(f"    {op['name']:<22} {len(trades):>4} trades  {days:>3} day(s)  "
            f"net {net:>+11,.2f}  ->  {name}")

    (data / "directory.json").write_text(
        json.dumps({"v": 1, "generatedAt": stamp, "operators": directory},
                   separators=(",", ":")), encoding="utf-8")
    written.append("directory.json")

    # ---- prune payloads for operators who are no longer on the roster -----
    for stale in data.glob("op_*.json"):
        if stale.name not in written:
            stale.unlink()
            say(f"    removed stale payload {stale.name}")

    # ---- report -----------------------------------------------------------
    say("")
    for name, n in sorted(before_start.items()):
        say(f"    {n} trade(s) before {name}'s start date — not published")
    if followers:
        say(f"    {followers} follower row(s) skipped — firm copy, not operator execution")
    if skipped_test:
        say(f"    {skipped_test} test-account trade(s) excluded")
    if malformed:
        say(f"    {malformed} malformed row(s) skipped")
    if unmapped:
        print("\n  WARNING  These handles are not on the roster and were NOT published:")
        for a, n in sorted(unmapped.items()):
            print(f"             {a:<14} {n:>4} trades")
        print("           Add them to config/roster.json and run the build again.\n")

    say(f"  Done — {len(roster)} payload(s) written to {data}\n")


def generate_password() -> None:
    import secrets
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    words = ["-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))]
    print(words[0])


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Sigma Dynamix operator portal.")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--password", action="store_true",
                    help="print a strong random password and exit")
    args = ap.parse_args()
    if args.password:
        generate_password()
        return
    build(args.root.resolve(), args.quiet)


if __name__ == "__main__":
    main()
