"""
load_reviews.py — Load Etsy reviews from reviews.json into raw and core tables.

Idempotent: skips reviews already present by order_id.
Reviews with no matching core.orders row are loaded into raw but not promoted to core.

Usage:
  python scripts/load_reviews.py
  python scripts/load_reviews.py --dry-run
  python scripts/load_reviews.py --file /path/to/reviews.json
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_CONFIG = {
    "host":     os.getenv("BUSINESS_DB_HOST", "10.0.0.16"),
    "port":     int(os.getenv("BUSINESS_DB_PORT", 5433)),
    "dbname":   os.getenv("BUSINESS_DB_NAME", "business"),
    "user":     os.getenv("BUSINESS_DB_USER"),
    "password": os.getenv("BUSINESS_DB_PASSWORD"),
}

# Synology-synced Business folder. On the MacBook the Drive client syncs it to
# .../SynologyDrive-HomeSync/Documents/Business; on the M6 Mac mini the same contents
# land directly in .../SynologyDrive-HomeSync. Override with ENR_BUSINESS_ROOT.
BUSINESS_ROOT = Path(
    os.getenv(
        "ENR_BUSINESS_ROOT",
        "/Users/bnieco/Library/CloudStorage/SynologyDrive-HomeSync/Documents/Business",
    )
)
DEFAULT_REVIEWS_FILE = BUSINESS_ROOT / "_Data" / "ENR" / "Etsy" / "reviews.json"


def parse_date(s: str | None):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def load(reviews_file: Path, dry_run: bool = False):
    with open(reviews_file) as f:
        records = json.load(f)
    print(f"Reviews file: {reviews_file.name} ({len(records)} records)")

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # -- raw.etsy_reviews -------------------------------------------------
    cur.execute("SELECT order_id FROM raw.etsy_reviews")
    existing_raw = {r["order_id"] for r in cur.fetchall()}

    new_raw = [r for r in records if r["order_id"] not in existing_raw]
    print(f"raw.etsy_reviews: {len(existing_raw)} existing, {len(new_raw)} to insert")

    if not dry_run:
        for r in new_raw:
            cur.execute("""
                INSERT INTO raw.etsy_reviews (order_id, reviewer, reviewed_at, star_rating, message)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING
            """, (
                r["order_id"],
                r.get("reviewer"),
                parse_date(r.get("date_reviewed")),
                r.get("star_rating"),
                r.get("message"),
            ))

    # -- core.reviews -----------------------------------------------------
    # Build source_order_id → core order_id map (etsy orders only)
    cur.execute("""
        SELECT order_id, source_order_id::bigint
        FROM core.orders
        WHERE source = 'etsy' AND source_order_id ~ '^[0-9]+$'
    """)
    order_map = {r["source_order_id"]: r["order_id"] for r in cur.fetchall()}

    cur.execute("SELECT source_order_id FROM core.reviews")
    existing_core = {r["source_order_id"] for r in cur.fetchall()}

    inserted_core = skipped_core = 0
    for r in new_raw:
        src_oid = r["order_id"]
        if src_oid in existing_core:
            skipped_core += 1
            continue
        core_order_id = order_map.get(src_oid)
        if not core_order_id:
            print(f"  WARNING: no core.orders match for Etsy order_id {src_oid} — raw only")
            skipped_core += 1
            continue

        if not dry_run:
            cur.execute("""
                INSERT INTO core.reviews
                    (order_id, source_order_id, reviewer, reviewed_at, star_rating, message)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_order_id) DO NOTHING
            """, (
                core_order_id,
                src_oid,
                r.get("reviewer"),
                parse_date(r.get("date_reviewed")),
                r.get("star_rating"),
                r.get("message"),
            ))
        inserted_core += 1

    print(f"core.reviews:     {inserted_core} to insert, {skipped_core} skipped")

    if not dry_run:
        conn.commit()
        print("Committed.")
    else:
        print("DRY RUN — no changes written.")

    conn.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Load Etsy reviews into business DB")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--file", type=Path, default=DEFAULT_REVIEWS_FILE)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    load(reviews_file=args.file, dry_run=args.dry_run)
