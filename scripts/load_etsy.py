"""
load_etsy.py — Etsy CSV exports → business.raw schema

Loads Etsy order, order item, and listings CSV files into:
  raw.etsy_orders
  raw.etsy_order_items
  raw.etsy_listings

Idempotent per file: tracks loaded files in raw.etsy_load_log to prevent
duplicate loads. Re-run safe — already-loaded files are skipped.

Usage:
  # Load all CSVs from the default Etsy data directory
  python scripts/load_etsy.py

  # Load from a specific directory
  python scripts/load_etsy.py --source-dir /path/to/folder

  # Load a single file explicitly
  python scripts/load_etsy.py --file /path/to/EtsySoldOrders2026.csv

  # Dry run (parse and report, don't insert)
  python scripts/load_etsy.py --dry-run

File naming conventions recognized:
  EtsySoldOrders*.csv       → raw.etsy_orders
  EtsySoldOrderItems*.csv   → raw.etsy_order_items
  EtsyListingsDownload*.csv → raw.etsy_listings
"""

import argparse
import os
import re
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host": os.getenv("BUSINESS_DB_HOST", "10.0.0.16"),
    "port": int(os.getenv("BUSINESS_DB_PORT", 5433)),
    "dbname": os.getenv("BUSINESS_DB_NAME", "business"),
    "user": os.getenv("BUSINESS_DB_USER"),
    "password": os.getenv("BUSINESS_DB_PASSWORD"),
}

# Synology-synced Business folder. Same path on the MacBook and the M6 Mac mini
# (on the M6, ~/Library/CloudStorage/SynologyDrive-HomeSync is a link to ~/SynologyDrive).
# ENR_BUSINESS_ROOT is an optional override; leave it unset on both hosts.
BUSINESS_ROOT = Path(
    os.getenv(
        "ENR_BUSINESS_ROOT",
        "/Users/bnieco/Library/CloudStorage/SynologyDrive-HomeSync/Documents/Business",
    )
)

# ETSY_DATA_DIR remains an explicit override; its default derives from BUSINESS_ROOT.
DEFAULT_SOURCE_DIR = Path(
    os.getenv("ETSY_DATA_DIR", str(BUSINESS_ROOT / "_Data" / "ENR" / "Etsy"))
)

FILE_PATTERNS = [
    (re.compile(r"EtsySoldOrderItems",    re.IGNORECASE), "raw.etsy_order_items", "load_order_items"),
    (re.compile(r"EtsySoldOrders",        re.IGNORECASE), "raw.etsy_orders",      "load_orders"),
    (re.compile(r"EtsyListingsDownload",  re.IGNORECASE), "raw.etsy_listings",    "load_listings"),
]


# ---------------------------------------------------------------------------
# Load log
# ---------------------------------------------------------------------------

ENSURE_LOG_TABLE = """
    CREATE TABLE IF NOT EXISTS raw.etsy_load_log (
        id              SERIAL PRIMARY KEY,
        source_file     VARCHAR NOT NULL UNIQUE,
        target_table    VARCHAR NOT NULL,
        row_count       INT,
        loaded_at       TIMESTAMPTZ DEFAULT NOW()
    )
"""

def file_already_loaded(cursor, filename: str) -> bool:
    cursor.execute("SELECT 1 FROM raw.etsy_load_log WHERE source_file = %s", (filename,))
    return cursor.fetchone() is not None

def record_load(cursor, filename: str, table: str, row_count: int):
    cursor.execute(
        "INSERT INTO raw.etsy_load_log (source_file, target_table, row_count) VALUES (%s, %s, %s)",
        (filename, table, row_count)
    )


# ---------------------------------------------------------------------------
# Column name normalization — Etsy headers → snake_case matching DDL
# ---------------------------------------------------------------------------

ORDERS_COLUMN_MAP = {
    "Sale Date": "sale_date",
    "Order ID": "order_id",
    "Buyer User ID": "buyer_user_id",
    "Full Name": "full_name",
    "First Name": "first_name",
    "Last Name": "last_name",
    "Number of Items": "number_of_items",
    "Payment Method": "payment_method",
    "Date Shipped": "date_shipped",
    "Street 1": "street_1",
    "Street 2": "street_2",
    "Ship City": "ship_city",
    "Ship State": "ship_state",
    "Ship Zipcode": "ship_zipcode",
    "Ship Country": "ship_country",
    "Currency": "currency",
    "Order Value": "order_value",
    "Coupon Code": "coupon_code",
    "Coupon Details": "coupon_details",
    "Discount Amount": "discount_amount",
    "Shipping Discount": "shipping_discount",
    "Shipping": "shipping",
    "Sales Tax": "sales_tax",
    "Order Total": "order_total",
    "Status": "status",
    "Card Processing Fees": "card_processing_fees",
    "Order Net": "order_net",
    "Adjusted Order Total": "adjusted_order_total",
    "Adjusted Card Processing Fees": "adjusted_card_processing_fees",
    "Adjusted Net Order Amount": "adjusted_net_order_amount",
    "Buyer": "buyer",
    "Order Type": "order_type",
    "Payment Type": "payment_type",
    "InPerson Discount": "inperson_discount",
    "InPerson Location": "inperson_location",
    "SKU": "sku",
}

ORDER_ITEMS_COLUMN_MAP = {
    "Sale Date": "sale_date",
    "Item Name": "item_name",
    "Buyer": "buyer",
    "Quantity": "quantity",
    "Price": "price",
    "Coupon Code": "coupon_code",
    "Coupon Details": "coupon_details",
    "Discount Amount": "discount_amount",
    "Shipping Discount": "shipping_discount",
    "Order Shipping": "order_shipping",
    "Order Sales Tax": "order_sales_tax",
    "Item Total": "item_total",
    "Currency": "currency",
    "Transaction ID": "transaction_id",
    "Listing ID": "listing_id",
    "Date Paid": "date_paid",
    "Date Shipped": "date_shipped",
    "Ship Name": "ship_name",
    "Ship Address1": "ship_address1",
    "Ship Address2": "ship_address2",
    "Ship City": "ship_city",
    "Ship State": "ship_state",
    "Ship Zipcode": "ship_zipcode",
    "Ship Country": "ship_country",
    "Order ID": "order_id",
    "Variations": "variations",
    "Order Type": "order_type",
    "Listings Type": "listings_type",
    "Payment Type": "payment_type",
    "InPerson Discount": "inperson_discount",
    "InPerson Location": "inperson_location",
    "VAT Paid by Buyer": "vat_paid_by_buyer",
    "SKU": "sku",
}

LISTINGS_COLUMN_MAP = {
    "TITLE": "title",
    "DESCRIPTION": "description",
    "PRICE": "price",
    "CURRENCY_CODE": "currency_code",
    "QUANTITY": "quantity",
    "TAGS": "tags",
    "MATERIALS": "materials",
    "IMAGE1": "image1",
    "IMAGE2": "image2",
    "IMAGE3": "image3",
    "IMAGE4": "image4",
    "IMAGE5": "image5",
    "IMAGE6": "image6",
    "IMAGE7": "image7",
    "IMAGE8": "image8",
    "IMAGE9": "image9",
    "IMAGE10": "image10",
    "VARIATION 1 TYPE": "variation_1_type",
    "VARIATION 1 NAME": "variation_1_name",
    "VARIATION 1 VALUES": "variation_1_values",
    "VARIATION 2 TYPE": "variation_2_type",
    "VARIATION 2 NAME": "variation_2_name",
    "VARIATION 2 VALUES": "variation_2_values",
    "SKU": "sku",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_dates(df: pd.DataFrame, date_cols: list) -> pd.DataFrame:
    """Parse Etsy date columns, tolerating both 2-digit (%m/%d/%y) and 4-digit
    (%m/%d/%Y) year formats. As of the 2026-05-23 export, Etsy mixes formats
    across columns: Sale Date is %m/%d/%y, Date Paid/Date Shipped are %m/%d/%Y.
    """
    for col in date_cols:
        if col in df.columns:
            # Try 2-digit year first; fill any NaT from 4-digit year format
            parsed = pd.to_datetime(df[col], errors="coerce", format="%m/%d/%y")
            mask_unparsed = parsed.isna() & df[col].notna()
            if mask_unparsed.any():
                parsed_4d = pd.to_datetime(
                    df.loc[mask_unparsed, col], errors="coerce", format="%m/%d/%Y"
                )
                parsed.loc[mask_unparsed] = parsed_4d
            df[col] = parsed.dt.date
    return df


def _insert(df: pd.DataFrame, table: str, cursor):
    # Convert NaN/NaT and pandas missing markers to None for clean SQL NULLs
    df = df.astype(object).where(pd.notna(df), None)
    records = df.to_dict("records")
    col_names = ", ".join(records[0].keys())
    placeholders = ", ".join(f"%({k})s" for k in records[0].keys())
    cursor.executemany(
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
        records
    )
    return len(records)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_orders(df: pd.DataFrame, source_file: str, cursor, dry_run: bool):
    df = df.rename(columns=ORDERS_COLUMN_MAP)
    df = parse_dates(df, ["sale_date", "date_shipped"])
    df["_source_file"] = source_file
    cols = list(ORDERS_COLUMN_MAP.values()) + ["_source_file"]
    df = df[[c for c in cols if c in df.columns]]
    if dry_run:
        print(f"  DRY RUN — {len(df)} rows would be inserted into raw.etsy_orders")
        return len(df)
    return _insert(df, "raw.etsy_orders", cursor)


def load_order_items(df: pd.DataFrame, source_file: str, cursor, dry_run: bool):
    df = df.rename(columns=ORDER_ITEMS_COLUMN_MAP)
    df = parse_dates(df, ["sale_date", "date_paid", "date_shipped"])
    df["_source_file"] = source_file
    cols = list(ORDER_ITEMS_COLUMN_MAP.values()) + ["_source_file"]
    df = df[[c for c in cols if c in df.columns]]
    if dry_run:
        print(f"  DRY RUN — {len(df)} rows would be inserted into raw.etsy_order_items")
        return len(df)
    return _insert(df, "raw.etsy_order_items", cursor)


def load_listings(df: pd.DataFrame, source_file: str, cursor, dry_run: bool):
    df = df.rename(columns=LISTINGS_COLUMN_MAP)
    df["_source_file"] = source_file
    cols = list(LISTINGS_COLUMN_MAP.values()) + ["_source_file"]
    df = df[[c for c in cols if c in df.columns]]
    if dry_run:
        print(f"  DRY RUN — {len(df)} rows would be inserted into raw.etsy_listings")
        return len(df)
    return _insert(df, "raw.etsy_listings", cursor)


LOADER_MAP = {
    "load_orders": load_orders,
    "load_order_items": load_order_items,
    "load_listings": load_listings,
}


# ---------------------------------------------------------------------------
# Routing + entry point
# ---------------------------------------------------------------------------

def route_file(filepath: Path):
    for pattern, table, loader_name in FILE_PATTERNS:
        if pattern.search(filepath.name):
            return table, LOADER_MAP[loader_name]
    return None, None


def process_file(filepath: Path, dry_run: bool = False):
    table, loader = route_file(filepath)
    if not loader:
        print(f"  SKIPPED (unrecognized filename pattern): {filepath.name}")
        return

    print(f"Processing {filepath.name} → {table}...")
    df = pd.read_csv(filepath, dtype=str)

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(ENSURE_LOG_TABLE)

            if not dry_run and file_already_loaded(cur, filepath.name):
                print(f"  Already loaded — skipping.")
                return

            row_count = loader(df, filepath.name, cur, dry_run)

            if not dry_run:
                record_load(cur, filepath.name, table, row_count)
                conn.commit()
                print(f"  Inserted {row_count} rows.")


def parse_args():
    parser = argparse.ArgumentParser(description="Load Etsy CSV exports into business.raw")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--source-dir", help=f"Directory to scan for CSV files (default: {DEFAULT_SOURCE_DIR})")
    group.add_argument("--file", help="Load a single specific file")
    parser.add_argument("--dry-run", action="store_true", help="Parse files but do not insert")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.file:
        process_file(Path(args.file), dry_run=args.dry_run)
    else:
        source_dir = Path(args.source_dir) if args.source_dir else DEFAULT_SOURCE_DIR
        csv_files = sorted(source_dir.glob("*.csv"))
        if not csv_files:
            print(f"No CSV files found in {source_dir}")
            return
        for f in csv_files:
            process_file(f, dry_run=args.dry_run)

    print("Done.")


if __name__ == "__main__":
    main()
