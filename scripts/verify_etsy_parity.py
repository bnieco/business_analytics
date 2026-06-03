"""verify_etsy_parity.py — one-command Etsy API ↔ CSV parity check.

Pre-staged 2026-06-03 (Edison). CANNOT run meaningfully until a first live API
load has populated the API-era columns (_api_loaded_at, listing_id, receipt_id).
Until then the "no API rows yet" verdict fires and tells you to load first.

What it does
------------
Compares what the Etsy Open API loader (scripts/etsy_api.py) wrote against the
CSV-era rows already in raw.etsy_* for the SAME window, to prove the API is a
safe replacement before retiring the manual CSV exports.

API vs CSV rows are distinguished by `_api_loaded_at`:
  * API rows  : _api_loaded_at IS NOT NULL
  * CSV rows  : _api_loaded_at IS NULL  (load_etsy.py never sets it)

Checks (over a comparison window, default: last 30 days of sale_date):
  1. Orders     — receipt/order count API vs CSV; money totals (order_total).
  2. Order items— transaction count API vs CSV; price totals.
  3. Listings   — API should be a SUPERSET of CSV and must carry listing_id.

Decision rule
-------------
  PASS              — API order/transaction counts >= CSV for the window, money
                      totals within tolerance, and listings are a superset with
                      listing_id populated. Safe to retire CSV after >=1 clean
                      reporting cycle.
  BLOCKER           — API is MISSING receipts/transactions the CSV has, or money
                      totals diverge beyond tolerance. Do NOT retire CSV; surface
                      the gap for Brian.
  NO_API_ROWS_YET   — no _api_loaded_at rows present; run a live load first.

Usage
-----
    ./venv/bin/python scripts/verify_etsy_parity.py
    ./venv/bin/python scripts/verify_etsy_parity.py --since 2026-05-01 --until 2026-06-03
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import pandas as pd

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import query  # noqa: E402

MONEY_TOLERANCE = 0.01  # absolute $ tolerance on summed money fields


ORDERS_SQL = """
SELECT
    COUNT(*) FILTER (WHERE _api_loaded_at IS NOT NULL)                       AS api_orders,
    COUNT(*) FILTER (WHERE _api_loaded_at IS NULL)                           AS csv_orders,
    COALESCE(SUM(order_total) FILTER (WHERE _api_loaded_at IS NOT NULL), 0)  AS api_order_total,
    COALESCE(SUM(order_total) FILTER (WHERE _api_loaded_at IS NULL), 0)      AS csv_order_total
FROM raw.etsy_orders
WHERE sale_date BETWEEN :since AND :until
"""

ITEMS_SQL = """
SELECT
    COUNT(*) FILTER (WHERE _api_loaded_at IS NOT NULL)                  AS api_items,
    COUNT(*) FILTER (WHERE _api_loaded_at IS NULL)                      AS csv_items,
    COALESCE(SUM(price) FILTER (WHERE _api_loaded_at IS NOT NULL), 0)   AS api_price_total,
    COALESCE(SUM(price) FILTER (WHERE _api_loaded_at IS NULL), 0)       AS csv_price_total
FROM raw.etsy_order_items
WHERE sale_date BETWEEN :since AND :until
"""

# Missing-receipt detail: CSV order_ids in the window not present among API rows.
MISSING_ORDERS_SQL = """
SELECT DISTINCT csv.order_id
FROM raw.etsy_orders csv
WHERE csv._api_loaded_at IS NULL
  AND csv.sale_date BETWEEN :since AND :until
  AND NOT EXISTS (
        SELECT 1 FROM raw.etsy_orders api
        WHERE api._api_loaded_at IS NOT NULL
          AND api.order_id = csv.order_id
  )
ORDER BY csv.order_id
LIMIT 50
"""

MISSING_ITEMS_SQL = """
SELECT DISTINCT csv.transaction_id
FROM raw.etsy_order_items csv
WHERE csv._api_loaded_at IS NULL
  AND csv.sale_date BETWEEN :since AND :until
  AND NOT EXISTS (
        SELECT 1 FROM raw.etsy_order_items api
        WHERE api._api_loaded_at IS NOT NULL
          AND api.transaction_id = csv.transaction_id
  )
ORDER BY csv.transaction_id
LIMIT 50
"""

LISTINGS_SQL = """
SELECT
    COUNT(*) FILTER (WHERE _api_loaded_at IS NOT NULL)                                   AS api_listings,
    COUNT(*) FILTER (WHERE _api_loaded_at IS NULL)                                       AS csv_listings,
    COUNT(*) FILTER (WHERE _api_loaded_at IS NOT NULL AND listing_id IS NOT NULL)        AS api_listings_with_id,
    COUNT(*) FILTER (WHERE _api_loaded_at IS NOT NULL AND listing_id IS NULL)            AS api_listings_missing_id
FROM raw.etsy_listings
"""


def _banner(title: str) -> None:
    bar = "=" * (len(title) + 8)
    print(f"\n{bar}\n=== {title} ===\n{bar}")


def _print_df(df: pd.DataFrame) -> None:
    print("(no rows)" if df.empty else df.to_string(index=False))


def parse_date_arg(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    p = argparse.ArgumentParser(description="Etsy API ↔ CSV parity check")
    p.add_argument("--since", type=parse_date_arg, help="Window start (YYYY-MM-DD)")
    p.add_argument("--until", type=parse_date_arg, help="Window end (YYYY-MM-DD)")
    args = p.parse_args()

    until = args.until or date.today()
    since = args.since or (until - timedelta(days=30))
    params = {"since": since, "until": until}

    print(f"\nEtsy API ↔ CSV parity check\n  window: {since} -> {until}")

    orders = query(ORDERS_SQL, **params)
    _banner("Orders (receipts)")
    _print_df(orders)

    items = query(ITEMS_SQL, **params)
    _banner("Order items (transactions)")
    _print_df(items)

    listings = query(LISTINGS_SQL)
    _banner("Listings (full set; listing_id coverage)")
    _print_df(listings)

    o = orders.iloc[0]
    it = items.iloc[0]
    ls = listings.iloc[0]

    api_orders = int(o["api_orders"])
    api_items = int(it["api_items"])

    # ---- verdict ----------------------------------------------------------
    if api_orders == 0 and api_items == 0 and int(ls["api_listings"]) == 0:
        _banner("VERDICT: NO_API_ROWS_YET")
        print("No API-loaded rows found (_api_loaded_at IS NULL everywhere).")
        print("Run a live load first:  ./venv/bin/python scripts/etsy_api.py")
        print("then re-run this parity check.")
        return 0

    blockers: list[str] = []

    csv_orders = int(o["csv_orders"])
    csv_items = int(it["csv_items"])
    if csv_orders and api_orders < csv_orders:
        blockers.append(f"API orders ({api_orders}) < CSV orders ({csv_orders}) — missing receipts.")
    if csv_items and api_items < csv_items:
        blockers.append(f"API items ({api_items}) < CSV items ({csv_items}) — missing transactions.")

    order_total_diff = abs(float(o["api_order_total"]) - float(o["csv_order_total"]))
    if csv_orders and order_total_diff > MONEY_TOLERANCE:
        blockers.append(
            f"order_total mismatch: API ${float(o['api_order_total']):.2f} vs "
            f"CSV ${float(o['csv_order_total']):.2f} (Δ ${order_total_diff:.2f})."
        )
    price_total_diff = abs(float(it["api_price_total"]) - float(it["csv_price_total"]))
    if csv_items and price_total_diff > MONEY_TOLERANCE:
        blockers.append(
            f"item price total mismatch: API ${float(it['api_price_total']):.2f} vs "
            f"CSV ${float(it['csv_price_total']):.2f} (Δ ${price_total_diff:.2f})."
        )

    if int(ls["api_listings_missing_id"]) > 0:
        blockers.append(
            f"{int(ls['api_listings_missing_id'])} API listing(s) have NULL listing_id — "
            "the core gap this migration was meant to close."
        )
    if int(ls["api_listings"]) and int(ls["api_listings"]) < int(ls["csv_listings"]):
        blockers.append(
            f"API listings ({int(ls['api_listings'])}) < CSV listings ({int(ls['csv_listings'])}) — "
            "expected a SUPERSET, not fewer."
        )

    if blockers:
        missing_orders = query(MISSING_ORDERS_SQL, **params)
        _banner("Missing CSV order_ids not in API (up to 50)")
        _print_df(missing_orders)
        missing_items = query(MISSING_ITEMS_SQL, **params)
        _banner("Missing CSV transaction_ids not in API (up to 50)")
        _print_df(missing_items)

        _banner("VERDICT: BLOCKER")
        for b in blockers:
            print(f"  - {b}")
        print("\nDo NOT retire the CSV pipeline. Resolve the gaps above, re-load, re-check.")
        return 1

    _banner("VERDICT: PASS")
    print(f"  API orders        : {api_orders}  (CSV {csv_orders})")
    print(f"  API items         : {api_items}  (CSV {csv_items})")
    print(f"  API listings      : {int(ls['api_listings'])} ({int(ls['api_listings_with_id'])} with listing_id)")
    print("\nAPI is a safe superset for this window. Keep CSV as fallback for")
    print(">=1 reporting cycle, then retire the manual exports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
