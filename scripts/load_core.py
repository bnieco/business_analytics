"""
load_core.py — Populate core.customers, core.orders, core.order_items
from raw.etsy_orders and raw.etsy_order_items.

Idempotent: skips orders already present by source_order_id.
Unresolved listing_ids are logged and skipped (not inserted).

Usage:
  python scripts/load_core.py
  python scripts/load_core.py --dry-run
"""

import argparse
import os
from decimal import Decimal
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

# Item-name → SKU for listing_id=0 (direct/in-person sales).
# Matched case-insensitively after stripping whitespace.
DIRECT_ITEM_MAP = {
    "deck of tokens for daggerheart":                             "DH-CSB-001",
    "elder's dice vault":                                         "DT-VPR-001",
    "ground - 1 tray":                                            "DT-GRD-001",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def coerce(val):
    """Return None for NaN / empty strings, else the value."""
    if val is None:
        return None
    s = str(val).strip()
    return None if s.lower() in ("nan", "") else s


def resolve_part(listing_id: str, item_name: str, part_map: dict, sku_to_part: dict) -> int | None:
    """Return part_id or None if unresolvable."""
    lid = coerce(listing_id)
    if lid and lid != "0":
        return part_map.get(lid)
    # listing_id is 0 or null — try item_name match
    key = (item_name or "").strip().lower()
    sku = DIRECT_ITEM_MAP.get(key)
    return sku_to_part.get(sku) if sku else None


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load(dry_run: bool = False):
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # -- Reference data ---------------------------------------------------
    cur.execute("SELECT listing_id, part_id FROM raw.listing_id_map WHERE part_id IS NOT NULL")
    part_map = {r["listing_id"]: r["part_id"] for r in cur.fetchall()}

    cur.execute("SELECT sku, part_id FROM core.parts")
    sku_to_part = {r["sku"]: r["part_id"] for r in cur.fetchall()}

    # -- Customers --------------------------------------------------------
    cur.execute("SELECT source_customer_key FROM core.customers WHERE source = 'etsy'")
    existing_customers = {r["source_customer_key"] for r in cur.fetchall()}

    # source_customer_key = buyer_user_id (Etsy handle) when available, else buyer name/email.
    cur.execute("""
        SELECT DISTINCT ON (ckey)
            ckey, full_name, buyer,
            MIN(sale_date) OVER (PARTITION BY ckey) AS first_seen,
            MAX(sale_date) OVER (PARTITION BY ckey) AS last_order
        FROM (
            SELECT *,
                CASE WHEN buyer_user_id IS NOT NULL AND buyer_user_id != 'NaN' THEN buyer_user_id
                     ELSE buyer
                END AS ckey
            FROM raw.etsy_orders
        ) sub
        WHERE ckey IS NOT NULL
        ORDER BY ckey, sale_date
    """)
    raw_customers = cur.fetchall()

    new_customers = [r for r in raw_customers if coerce(r["ckey"]) not in existing_customers]
    print(f"Customers: {len(existing_customers)} existing, {len(new_customers)} to insert")

    if not dry_run:
        for r in new_customers:
            cur.execute("""
                INSERT INTO core.customers
                    (source, source_customer_key, display_name, first_seen_at, last_order_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                "etsy",
                coerce(r["ckey"]),
                coerce(r["full_name"]) or coerce(r["buyer"]),
                r["first_seen"],
                r["last_order"],
            ))

    # Build buyer_user_id → customer_id map.
    # In dry-run mode customers weren't written, so synthesise the map from raw data.
    if dry_run:
        customer_map = {coerce(r["ckey"]): -1 for r in raw_customers if coerce(r["ckey"])}
    else:
        cur.execute("SELECT customer_id, source_customer_key FROM core.customers WHERE source = 'etsy'")
        customer_map = {r["source_customer_key"]: r["customer_id"] for r in cur.fetchall()}

    # -- Orders -----------------------------------------------------------
    cur.execute("SELECT source_order_id FROM core.orders WHERE source = 'etsy'")
    existing_orders = {r["source_order_id"] for r in cur.fetchall()}

    cur.execute("SELECT * FROM raw.etsy_orders")
    raw_orders = cur.fetchall()
    new_orders = [r for r in raw_orders if coerce(r["order_id"]) not in existing_orders]
    print(f"Orders:    {len(existing_orders)} existing, {len(new_orders)} to insert")

    order_id_map = {}  # source_order_id → core order_id
    skipped_orders = []

    for r in new_orders:
        buyer_key = coerce(r["buyer_user_id"]) or coerce(r["buyer"])
        customer_id = customer_map.get(buyer_key)
        if not customer_id:
            skipped_orders.append((r["order_id"], "no customer_id resolved"))
            continue

        date_shipped = r["date_shipped"]
        status = "fulfilled" if date_shipped else "open"

        if not dry_run:
            cur.execute("""
                INSERT INTO core.orders
                    (source, source_order_id, customer_id, ordered_at, fulfilled_at,
                     status, subtotal, shipping, tax, total)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING order_id
            """, (
                "etsy",
                coerce(r["order_id"]),
                customer_id,
                r["sale_date"],
                date_shipped,
                status,
                r["order_value"]  or Decimal("0"),
                r["shipping"]     or Decimal("0"),
                r["sales_tax"]    or Decimal("0"),
                r["order_total"]  or Decimal("0"),
            ))
            order_id_map[coerce(r["order_id"])] = cur.fetchone()["order_id"]

    if skipped_orders:
        print(f"  Skipped {len(skipped_orders)} orders (no customer resolved):")
        for oid, reason in skipped_orders:
            print(f"    order {oid}: {reason}")

    # Populate order_id_map for existing orders too
    if order_id_map or existing_orders:
        cur.execute("SELECT order_id, source_order_id FROM core.orders WHERE source = 'etsy'")
        order_id_map = {r["source_order_id"]: r["order_id"] for r in cur.fetchall()}

    # -- Order Items ------------------------------------------------------
    cur.execute("SELECT raw_listing_id, order_id FROM core.order_items")
    existing_items = {(str(r["raw_listing_id"]), r["order_id"]) for r in cur.fetchall()}

    cur.execute("SELECT * FROM raw.etsy_order_items")
    raw_items = cur.fetchall()

    inserted_items = skipped_items = 0

    for r in raw_items:
        src_order_id = coerce(r["order_id"])
        core_order_id = order_id_map.get(src_order_id)
        if not core_order_id:
            skipped_items += 1
            continue

        part_id = resolve_part(coerce(r["listing_id"]), r["item_name"], part_map, sku_to_part)
        if not part_id:
            print(f"  UNRESOLVED item: listing_id={r['listing_id']!r}  title={str(r['item_name'])[:60]!r}")
            skipped_items += 1
            continue

        if dry_run:
            inserted_items += 1
            continue

        cur.execute("""
            INSERT INTO core.order_items
                (order_id, part_id, quantity, unit_price, line_total, raw_title, raw_listing_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            core_order_id,
            part_id,
            int(r["quantity"] or 1),
            r["price"]      or Decimal("0"),
            r["item_total"] or Decimal("0"),
            coerce(r["item_name"]),
            int(r["listing_id"]) if coerce(r["listing_id"]) and coerce(r["listing_id"]) != "0" else None,
        ))
        inserted_items += 1

    print(f"Order items: {inserted_items} inserted, {skipped_items} skipped")

    if not dry_run:
        conn.commit()
        print("Committed.")
    else:
        print("DRY RUN — no changes written.")

    conn.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Load core entities from raw Etsy tables")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    load(dry_run=args.dry_run)
