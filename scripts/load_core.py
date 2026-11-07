"""
load_core.py — Populate core.customers, core.orders, core.order_items
from raw.etsy_orders and raw.etsy_order_items.

Idempotent: skips orders already present by source_order_id, except that
existing orders still 'open' are re-synced (status + fulfilled_at) from the
latest raw.etsy_orders row once Etsy reports them shipped or canceled.
Unresolved listing_ids are logged and skipped (not inserted).

Usage:
  python scripts/load_core.py
  python scripts/load_core.py --dry-run
"""

import argparse
import os
import unicodedata
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

# Item-name → SKU for listing_id null/0 (direct/in-person sales, or Etsy rows
# whose listing_id didn't come through).
#
# Keys are NORMALIZED (see normalize_name): casefolded, Unicode-folded so curly
# apostrophes (’) and en/em-dashes (–/—) match their ASCII forms, and whitespace
# collapsed. Matching is by normalized-PREFIX (normalize_name(item) starts with
# the key) so long marketing-suffix titles like
#   "Elder's Dice Vault – Solid Wood Modular TTRPG Dice Vault & Rolling Tray – Cherry or Walnut"
# resolve via the short canonical key. List longer/more-specific keys first;
# resolve_part takes the longest matching key.
DIRECT_ITEM_MAP = {
    # "Deck of Tokens for Daggerheart …" — Brian-confirmed same canonical part as
    # listings 4334519499 / 4332440465 (DH-CSB-001, part 1). See seed_parts.sql.
    "deck of tokens for daggerheart":                            "DH-CSB-001",
    # "Elder's Dice Vault – Solid Wood … Dice Vault & Rolling Tray – Cherry or
    # Walnut" ($60) is an earlier title variant of DT-VLT-001 (listing 4332433907,
    # part 4), NOT DT-VPR-001. Per vault "DT-VPR-001 Catalog Listing ID Fix":
    # DT-VPR-001 ("Elder's Vault of Protection", listing 4332484207) has
    # Brian-confirmed GENUINE ZERO SALES, so this sale cannot belong to it.
    "elder's dice vault":                                        "DT-VLT-001",
    "ground - 1 tray":                                           "DT-GRD-001",
    # "Custom Solid Wood and Brass Plinko Game" ($150 x 2, order 4028192890) is a
    # one-off custom build with listing_id NULL, so it resolves by title prefix.
    # See seed_parts.sql (CW-PLK-001). Brian's call: model it as its own part.
    "custom solid wood and brass plinko game":                   "CW-PLK-001",
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


# Unicode punctuation that varies between Etsy title revisions but should NOT
# block a DIRECT_ITEM_MAP match. Folded to their ASCII equivalents before lookup.
_PUNCT_FOLD = {
    "’": "'",  # right single quote / curly apostrophe ’ -> '
    "‘": "'",  # left single quote ‘ -> '
    "ʼ": "'",  # modifier letter apostrophe ʼ -> '
    "–": "-",  # en dash – -> -
    "—": "-",  # em dash — -> -
    "‐": "-",  # hyphen ‐ -> -
    " ": " ",  # non-breaking space -> space
}


def normalize_name(name: str) -> str:
    """Casefold + fold Unicode punctuation + collapse whitespace.

    Makes DIRECT_ITEM_MAP matching robust to the apostrophe/dash/whitespace
    variations that appear across Etsy title revisions (e.g. ’ vs ', – vs -),
    so this class of skip can't recur from punctuation alone.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = "".join(_PUNCT_FOLD.get(ch, ch) for ch in s)
    return " ".join(s.casefold().split())


def resolve_part(listing_id: str, item_name: str, part_map: dict, sku_to_part: dict) -> int | None:
    """Return part_id or None if unresolvable.

    listing_id (when present, non-zero) wins via raw.listing_id_map. Otherwise
    fall back to a normalized prefix match against DIRECT_ITEM_MAP — the longest
    matching key wins, so a specific key is preferred over a shorter one.
    """
    lid = coerce(listing_id)
    if lid and lid != "0":
        return part_map.get(lid)
    # listing_id is null/0 — match item_name against DIRECT_ITEM_MAP (normalized).
    norm = normalize_name(item_name)
    if not norm:
        return None
    sku = None
    best_len = -1
    for key, key_sku in DIRECT_ITEM_MAP.items():
        nkey = normalize_name(key)
        if (norm == nkey or norm.startswith(nkey + " ")) and len(nkey) > best_len:
            sku, best_len = key_sku, len(nkey)
    return sku_to_part.get(sku) if sku else None


def derive_status(raw_order) -> tuple[str, object]:
    """Return (core status, fulfilled_at) for a raw.etsy_orders row.

    Shipped when Etsy reports is_shipped OR a shipment date exists (some
    shipped receipts, e.g. custom/hand-delivered, have is_shipped=True but no
    shipment timestamp, so date_shipped alone undercounts). fulfilled_at is the
    shipment date when known, else NULL. Canceled receipts map to 'cancelled'.
    """
    date_shipped = raw_order.get("date_shipped")
    raw_status = (coerce(raw_order.get("status")) or "").lower()
    if date_shipped or raw_order.get("is_shipped") is True:
        return "fulfilled", date_shipped
    if raw_status in ("canceled", "cancelled"):
        return "cancelled", None
    return "open", None


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

        status, date_shipped = derive_status(r)

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

    # -- Status re-sync for existing open orders ---------------------------
    # Without this, status/fulfilled_at were frozen at first ingest, so an
    # order that was 'open' when first loaded stayed 'open' after it shipped.
    # Only 'open' rows move; fulfilled/cancelled rows are never touched.
    raw_by_id = {coerce(r["order_id"]): r for r in raw_orders}
    cur.execute("""
        SELECT order_id, source_order_id FROM core.orders
        WHERE source = 'etsy' AND status = 'open'
    """)
    status_updates = []
    for o in cur.fetchall():
        raw = raw_by_id.get(o["source_order_id"])
        if raw is None:
            continue
        new_status, fulfilled_at = derive_status(raw)
        if new_status != "open":
            status_updates.append((o["order_id"], o["source_order_id"], new_status, fulfilled_at))
    print(f"Status re-sync: {len(status_updates)} open order(s) to update")
    for core_id, src_id, new_status, fulfilled_at in status_updates:
        print(f"  order {core_id} (receipt {src_id}): open -> {new_status}, fulfilled_at={fulfilled_at}")
        if not dry_run:
            cur.execute("""
                UPDATE core.orders SET status = %s, fulfilled_at = %s
                WHERE order_id = %s AND status = 'open'
            """, (new_status, fulfilled_at, core_id))

    # Populate order_id_map for existing orders too
    if order_id_map or existing_orders:
        cur.execute("SELECT order_id, source_order_id FROM core.orders WHERE source = 'etsy'")
        order_id_map = {r["source_order_id"]: r["order_id"] for r in cur.fetchall()}

    # -- Order Items ------------------------------------------------------
    # Dedup key includes part_id so two distinct null-listing_id line items on
    # the same order (e.g. a "Ground - 1 Tray" plus an "Elder's Dice Vault",
    # both listing_id NULL) are not collapsed into one and wrongly skipped.
    cur.execute("SELECT raw_listing_id, part_id, order_id FROM core.order_items")
    existing_items = {
        (str(r["raw_listing_id"]), r["part_id"], r["order_id"]) for r in cur.fetchall()
    }

    cur.execute("SELECT * FROM raw.etsy_order_items")
    raw_items = cur.fetchall()

    inserted_items = skipped_items = existing_items_n = 0

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

        # Idempotency guard: skip rows already in core.order_items. Key must
        # match how raw_listing_id is stored on insert below (int, else None)
        # and how existing_items was built: (str(raw_listing_id), part_id, order_id).
        lid_for_key = (
            int(coerce(r["listing_id"]))
            if coerce(r["listing_id"]) and coerce(r["listing_id"]) != "0"
            else None
        )
        if (str(lid_for_key), part_id, core_order_id) in existing_items:
            existing_items_n += 1
            continue

        if dry_run:
            inserted_items += 1
            continue

        # line_total convention: the Etsy v3 API transaction object carries only a
        # per-unit `price` (mapped to raw.price), NOT a line-extended total — so
        # raw.item_total is NULL for every API-ingested row. Derive line_total as
        # quantity * unit_price, matching how the CSV-era good rows (<=130) were
        # computed. Fall back to raw.item_total when it IS present (legacy CSV
        # loads) so historical behaviour is preserved. Order-level discounts are
        # intentionally NOT applied at the line (they live on core.orders.subtotal),
        # consistent with the existing good rows.
        quantity = int(r["quantity"] or 1)
        unit_price = r["price"] or Decimal("0")
        line_total = r["item_total"]
        if line_total is None:
            line_total = unit_price * quantity

        cur.execute("""
            INSERT INTO core.order_items
                (order_id, part_id, quantity, unit_price, line_total, raw_title, raw_listing_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            core_order_id,
            part_id,
            quantity,
            unit_price,
            line_total,
            coerce(r["item_name"]),
            int(r["listing_id"]) if coerce(r["listing_id"]) and coerce(r["listing_id"]) != "0" else None,
        ))
        inserted_items += 1

    print(f"Order items: {inserted_items} inserted, {existing_items_n} already present, {skipped_items} skipped (unresolved)")

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
