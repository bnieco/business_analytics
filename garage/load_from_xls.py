"""One-shot, idempotent loader: garage spreadsheet -> inventory.garage_items.

Reads the canonical `.xls` STRICTLY READ-ONLY: the file is copied to a temp
directory and converted there with LibreOffice headless (`xlrd` is not in the
venv, so pandas cannot read `.xls` directly); the canonical file is never
opened for writing, never modified, never moved.

Load policy (Garage Inventory App scope §3 + the 2026-07-02 decisions):
  * Only rows with a non-blank Room are real (639 of 2,192); the pre-numbered
    placeholder rows (Index 643-2195, blank Room) are DROPPED, not migrated.
  * `idx` (spreadsheet Index) and `loc_code` (Loc Code) are preserved EXACTLY.
  * Upsert keyed on `idx` (INSERT ... ON CONFLICT (idx) DO UPDATE) so re-runs
    converge instead of duplicating.
  * Validation REPORTS, it does not silently fix:
      - a Room cell containing embedded tabs/newlines (the known
        "Kitchen\\tCabinet 8\\tResveratrol" row) is QUARANTINED — skipped and
        printed in full for Brian to supply the intended values;
      - duplicate idx / loc_code aborts the load (none exist today);
      - a loc_code that violates first(Room)+first(Location)+Index is
        quarantined as drift — EXCEPT the two known blank-Location rows
        (G573, G613), which match first(Room)+Index and load as-is;
      - Room casing variants are reported, NOT normalized.
  * After load, the idx sequence is set so nextval() = max(loaded idx)+1,
    floored at 643 (the decided start; placeholders were never physical).

Run (the authorized production write):
    ./venv/bin/python -m garage.load_from_xls
Options:
    --xls PATH   source spreadsheet (default: the canonical File Cabinet path)
    --dry-run    validate + report only; no DB writes
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import psycopg2.extras

from garage import db

CANONICAL_XLS = Path(
    "/Volumes/File Cabinet/Labels/My Labels/Label Inventory xls format.xls"
)
SOFFICE_CANDIDATES = [
    "soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]
SEQ_FLOOR = 642  # setval floor -> nextval() is at least 643 (decided start)

COLUMNS = [  # spreadsheet header -> db column
    ("Room", "room"),
    ("Location", "location"),
    ("Label Line 1", "label_line1"),
    ("Label Line 2", "label_line2"),
    ("Media", "media"),
    ("Was", "former_location"),
    ("Number", "qty"),
    ("Loc Code", "loc_code"),
    ("Type", "item_type"),
    ("Index", "idx"),
]

UPSERT_SQL = f"""
INSERT INTO {db.TABLE}
    (idx, loc_code, room, location, label_line1, label_line2,
     media, former_location, qty, item_type)
VALUES (%(idx)s, %(loc_code)s, %(room)s, %(location)s, %(label_line1)s,
        %(label_line2)s, %(media)s, %(former_location)s, %(qty)s, %(item_type)s)
ON CONFLICT (idx) DO UPDATE SET
    loc_code        = EXCLUDED.loc_code,
    room            = EXCLUDED.room,
    location        = EXCLUDED.location,
    label_line1     = EXCLUDED.label_line1,
    label_line2     = EXCLUDED.label_line2,
    media           = EXCLUDED.media,
    former_location = EXCLUDED.former_location,
    qty             = EXCLUDED.qty,
    item_type       = EXCLUDED.item_type,
    updated_at      = now()
"""


def _find_soffice() -> str:
    for cand in SOFFICE_CANDIDATES:
        if shutil.which(cand) or Path(cand).exists():
            return cand
    sys.exit("ERROR: LibreOffice (soffice) not found; cannot convert the .xls.")


def read_sheet_readonly(xls_path: Path) -> pd.DataFrame:
    """Copy the .xls to a temp dir, convert to .xlsx there, read Sheet1."""
    if not xls_path.exists():
        sys.exit(f"ERROR: spreadsheet not found: {xls_path} (volume mounted?)")
    with tempfile.TemporaryDirectory(prefix="garage_load_") as tmp:
        tmp_xls = Path(tmp) / xls_path.name
        shutil.copy2(xls_path, tmp_xls)
        subprocess.run(
            [_find_soffice(), "--headless", "--convert-to", "xlsx",
             "--outdir", tmp, str(tmp_xls)],
            check=True, capture_output=True,
        )
        tmp_xlsx = tmp_xls.with_suffix(".xlsx")
        return pd.read_excel(tmp_xlsx, sheet_name="Sheet1")


def _clean(v):
    """NaN -> None; strings stripped of leading/trailing whitespace only."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def prepare_rows(df: pd.DataFrame):
    """Filter to real rows, validate, split into (loadable, quarantined)."""
    rows, quarantined, notes = [], [], []

    for _, r in df.iterrows():
        rec = {}
        for sheet_col, db_col in COLUMNS:
            rec[db_col] = _clean(r.get(sheet_col))
        if rec["room"] is None:
            continue  # pre-numbered placeholder slot — dropped by decision
        rec["idx"] = int(rec["idx"])
        rec["loc_code"] = str(rec["loc_code"])
        if rec["qty"] is not None:
            # Spreadsheet "Number" is typed integer in the DB, but the source
            # holds at least one non-numeric value (idx 344: 'w'). Report and
            # load the row with qty NULL — don't guess, don't drop the row.
            try:
                rec["qty"] = int(rec["qty"])
            except (TypeError, ValueError):
                notes.append(f"non-numeric Number {rec['qty']!r} on idx "
                             f"{rec['idx']} ({rec['loc_code']}) -> qty NULL")
                rec["qty"] = None

        # Quarantine 1: embedded tab/newline in Room (the known malformed row).
        if any(c in rec["room"] for c in ("\t", "\n")):
            quarantined.append(("malformed Room cell (embedded tabs)", rec))
            continue

        # Quarantine 2: loc_code drift vs the SOP rule. Blank-Location rows are
        # legitimate legacy exceptions if they match first(Room)+idx.
        if rec["location"]:
            expected = db.mint_loc_code(rec["room"], rec["location"], rec["idx"])
            if rec["loc_code"] != expected:
                quarantined.append(
                    (f"loc_code {rec['loc_code']!r} != rule {expected!r}", rec))
                continue
        else:
            expected = f"{rec['room'].strip()[0].upper()}{rec['idx']}"
            if rec["loc_code"] != expected:
                quarantined.append(
                    (f"blank Location and loc_code {rec['loc_code']!r} != "
                     f"{expected!r}", rec))
                continue
            notes.append(f"blank Location preserved as-is: idx {rec['idx']} "
                         f"({rec['loc_code']}), Room {rec['room']!r}")

        rows.append(rec)

    # Hard stops: duplicate identity would corrupt the physical-label contract.
    for key in ("idx", "loc_code"):
        seen, dups = set(), set()
        for rec in rows:
            (dups if rec[key] in seen else seen).add(rec[key])
        if dups:
            sys.exit(f"ABORT: duplicate {key} in source rows: {sorted(dups)}")

    # Report (not normalize) Room casing variants.
    by_ci = {}
    for rec in rows:
        by_ci.setdefault(rec["room"].lower(), set()).add(rec["room"])
    rooms = sorted(v.pop() for v in by_ci.values() if len(v) == 1)
    notes.append(f"distinct Rooms loaded: {rooms}")
    ci_variants = {k: sorted(v) for k, v in by_ci.items() if len(v) > 1}
    if ci_variants:
        notes.append(f"Room casing variants (NOT normalized): {ci_variants}")

    # Index gaps in the real range (507 and 548 are expected).
    idxs = sorted(rec["idx"] for rec in rows) + sorted(
        q["idx"] for _, q in quarantined)
    idxs = sorted(set(idxs))
    gaps = [i for i in range(idxs[0], idxs[-1] + 1) if i not in set(idxs)]
    notes.append(f"idx range {idxs[0]}-{idxs[-1]}, gaps: {gaps}")

    return rows, quarantined, notes


def load(rows, dry_run: bool) -> None:
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=200)
            max_idx = max(r["idx"] for r in rows)
            cur.execute(
                f"SELECT setval('{db.IDX_SEQ}', GREATEST(%s, %s), true)",
                (max_idx, SEQ_FLOOR),
            )
            cur.execute(f"SELECT count(*) FROM {db.TABLE}")
            total = cur.fetchone()[0]
            if dry_run:
                conn.rollback()
                print(f"[dry-run] would leave table at {total} rows; rolled back.")
            else:
                print(f"table now holds {total} rows; sequence nextval will be "
                      f">= {max(max_idx, SEQ_FLOOR) + 1}.")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--xls", type=Path, default=CANONICAL_XLS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    df = read_sheet_readonly(args.xls)
    rows, quarantined, notes = prepare_rows(df)

    print(f"source data rows: {len(df)}")
    print(f"real rows (non-blank Room): {len(rows) + len(quarantined)}")
    print(f"loadable: {len(rows)}   quarantined: {len(quarantined)}")
    for reason, rec in quarantined:
        print(f"  QUARANTINED idx {rec['idx']} ({rec['loc_code']}): {reason}")
        print(f"    full row: {rec}")
    for n in notes:
        print(f"  note: {n}")

    load(rows, args.dry_run)


if __name__ == "__main__":
    main()
