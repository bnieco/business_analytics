"""
load_etsy_traffic_stats.py — Etsy referrer-detail PDF data → raw.etsy_traffic_stats

Background
----------
Etsy Shop Manager > Stats > Traffic details exposes referrer information across
6 top-level categories (Etsy Search, Etsy App, Etsy Marketing & SEO,
Advertising, Social Media, Direct & Other). The only export format is a PDF
of each category page. PDFs are paginated and only page-1 of each table is
captured by the export — for categories with long sub-source lists, the
captured sub-sources do not sum to the dashboard category total.

This loader inserts data from a hand-curated JSON manifest
(scripts/data/etsy_traffic_stats_manifest.json) which combines:
  - per-(year, category, sub_source) visit counts extracted from each PDF
  - backfilled "Other (uncaptured)" rows so per-category sums reconcile to
    the dashboard screenshot category totals supplied by Brian
  - category-total rows (sub_source = NULL) for the 2 categories that have
    no breakdown table at all (Etsy Search, Etsy App)

The manifest is the deterministic source of truth. PDF parsing was performed
manually via Read-tool rendering during initial extraction; future re-imports
should regenerate the manifest from updated PDFs (or extend this loader with
a pdfplumber-based parser once that dependency is added to the venv).

Idempotency
-----------
Uses ON CONFLICT (year, category, sub_source) DO UPDATE on the unique index
(with NULLS NOT DISTINCT) so re-running refreshes visit counts and notes
without duplicating rows.

Usage
-----
  # Default: load from scripts/data/etsy_traffic_stats_manifest.json
  python scripts/load_etsy_traffic_stats.py

  # Use a different manifest file
  python scripts/load_etsy_traffic_stats.py --manifest /path/to/manifest.json

  # Dry run (parse + validate, no insert)
  python scripts/load_etsy_traffic_stats.py --dry-run

  # Verbose: print per-row insert plan
  python scripts/load_etsy_traffic_stats.py --verbose

The --input-dir flag is accepted for parity with sibling loaders but is
currently a no-op (manifest already lists per-PDF provenance). A future
extension will optionally re-extract from PDFs in --input-dir when
pdfplumber becomes available.
"""

import argparse
import json
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_MANIFEST = PROJECT_ROOT / "scripts" / "data" / "etsy_traffic_stats_manifest.json"
# Synology-synced Business folder. On the MacBook the Drive client syncs it to
# .../SynologyDrive-HomeSync/Documents/Business; on the M6 Mac mini the same contents
# land directly in .../SynologyDrive-HomeSync. Override with ENR_BUSINESS_ROOT.
BUSINESS_ROOT = Path(
    os.getenv(
        "ENR_BUSINESS_ROOT",
        "/Users/bnieco/Library/CloudStorage/SynologyDrive-HomeSync/Documents/Business",
    )
)
DEFAULT_INPUT_DIR = BUSINESS_ROOT / "Inbox"

DB_CONFIG = {
    "host":     os.getenv("BUSINESS_DB_HOST", "10.0.0.16"),
    "port":     int(os.getenv("BUSINESS_DB_PORT", 5433)),
    "dbname":   os.getenv("BUSINESS_DB_NAME", "business"),
    "user":     os.getenv("BUSINESS_DB_USER"),
    "password": os.getenv("BUSINESS_DB_PASSWORD"),
}

UPSERT_SQL = """
INSERT INTO raw.etsy_traffic_stats (
    year, category, sub_source, visits,
    date_range_start, date_range_end,
    source_pdf, notes
) VALUES (
    %(year)s, %(category)s, %(sub_source)s, %(visits)s,
    %(date_range_start)s, %(date_range_end)s,
    %(source_pdf)s, %(notes)s
)
ON CONFLICT (year, category, sub_source) DO UPDATE SET
    visits           = EXCLUDED.visits,
    date_range_start = EXCLUDED.date_range_start,
    date_range_end   = EXCLUDED.date_range_end,
    source_pdf       = EXCLUDED.source_pdf,
    notes            = EXCLUDED.notes,
    loaded_at        = NOW()
"""

VALID_CATEGORIES = {
    "Etsy Search",
    "Etsy App",
    "Etsy Marketing SEO",
    "Advertising",
    "Social Media",
    "Direct & Other",
}


def load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_rows(manifest: dict) -> list[dict]:
    """Validate manifest rows, return normalized list of records."""
    rows = manifest.get("rows", [])
    if not rows:
        raise ValueError("Manifest has no 'rows' array")

    normalized = []
    seen_keys: set[tuple] = set()

    for i, raw_row in enumerate(rows):
        # Required fields
        for fld in ("year", "category", "visits"):
            if fld not in raw_row:
                raise ValueError(f"Row {i}: missing required field '{fld}'")

        category = raw_row["category"]
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"Row {i}: category '{category}' not in {sorted(VALID_CATEGORIES)}"
            )

        # sub_source is optional/nullable
        sub_source = raw_row.get("sub_source")

        key = (raw_row["year"], category, sub_source)
        if key in seen_keys:
            raise ValueError(f"Row {i}: duplicate key {key}")
        seen_keys.add(key)

        rec = {
            "year":             int(raw_row["year"]),
            "category":         category,
            "sub_source":       sub_source,
            "visits":           int(raw_row["visits"]),
            "date_range_start": raw_row.get("date_range_start"),
            "date_range_end":   raw_row.get("date_range_end"),
            "source_pdf":       raw_row.get("source_pdf"),
            "notes":            raw_row.get("notes"),
        }
        normalized.append(rec)

    return normalized


def reconcile(records: list[dict], manifest: dict) -> None:
    """Sum per-(year, category) and warn if totals diverge from dashboard."""
    expected = manifest.get("_meta", {}).get("dashboard_category_totals", {})
    actual: dict[tuple, int] = {}
    for r in records:
        key = (str(r["year"]), r["category"])
        actual[key] = actual.get(key, 0) + r["visits"]

    print("Reconciliation (loaded total vs dashboard screenshot):")
    for year_str, cats in expected.items():
        for category, expected_total in cats.items():
            loaded = actual.get((year_str, category), 0)
            delta = loaded - expected_total
            flag = "OK " if delta == 0 else f"DIFF {delta:+d}"
            print(f"  {flag}  {year_str} {category:<22}  loaded={loaded:>6}  dashboard={expected_total:>6}")


def insert_rows(records: list[dict], dry_run: bool, verbose: bool) -> int:
    if verbose:
        for r in records:
            print(
                f"  {r['year']}  {r['category']:<22}  "
                f"{(r['sub_source'] or '<NULL>'):<45}  visits={r['visits']}"
            )

    if dry_run:
        print(f"DRY RUN — {len(records)} rows would be upserted.")
        return len(records)

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, records)
            inserted = cur.rowcount
        conn.commit()
    return inserted


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load Etsy referrer-detail PDF data into raw.etsy_traffic_stats"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Path to the JSON manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Inbox directory containing the source PDFs (currently informational only; "
             "manifest carries the parsed data). Default: %(default)s",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate the manifest, print reconciliation, do not insert.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every row before inserting.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"Loading manifest: {args.manifest}")
    manifest = load_manifest(args.manifest)
    records = validate_rows(manifest)
    print(f"Validated {len(records)} rows across "
          f"{len({(r['year'], r['category']) for r in records})} (year, category) groups.")

    # Sanity-check that referenced PDFs exist in --input-dir (warn only)
    if args.input_dir.exists():
        pdfs_referenced = {r["source_pdf"] for r in records if r.get("source_pdf")}
        missing = [p for p in pdfs_referenced if not (args.input_dir / p).exists()]
        if missing:
            print(f"  WARN: {len(missing)} PDF(s) referenced by manifest not found in {args.input_dir}:")
            for p in missing:
                print(f"    - {p}")

    reconcile(records, manifest)

    n = insert_rows(records, dry_run=args.dry_run, verbose=args.verbose)
    if args.dry_run:
        print("Done (dry run — no DB writes).")
    else:
        print(f"Done. Upserted {n} rows into raw.etsy_traffic_stats.")


if __name__ == "__main__":
    main()
