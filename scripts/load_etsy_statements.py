"""
load_etsy_statements.py — Etsy monthly statement CSVs → raw.etsy_statements

Each statement CSV (filename pattern: etsy_statement_YYYY_M.csv) is a per-month
ledger of activity: sales, fees, marketing (Etsy Ads / Offsite Ads commission),
shipping labels, deposits, etc.

Idempotent: uses UNIQUE(source_file, line_number) in the target table with
ON CONFLICT DO NOTHING. Re-running against the same file is a no-op.

Usage:
  # Load all statement CSVs from a directory
  python scripts/load_etsy_statements.py --source-dir /path/to/folder

  # Load a single statement
  python scripts/load_etsy_statements.py --file /path/to/etsy_statement_2026_3.csv

  # Dry run (parse and report, don't insert)
  python scripts/load_etsy_statements.py --source-dir /path/to/folder --dry-run

File name convention:
  etsy_statement_YYYY_M.csv  (M is 1-12, no leading zero)
"""

import argparse
import os
import re
from datetime import date
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_CONFIG = {
    "host": os.getenv("BUSINESS_DB_HOST", "10.0.0.16"),
    "port": int(os.getenv("BUSINESS_DB_PORT", 5433)),
    "dbname": os.getenv("BUSINESS_DB_NAME", "business"),
    "user": os.getenv("BUSINESS_DB_USER"),
    "password": os.getenv("BUSINESS_DB_PASSWORD"),
}

STATEMENT_FILENAME_RE = re.compile(r"^etsy_statement_(\d{4})_(\d{1,2})\.csv$", re.IGNORECASE)

# Source CSV columns → DB columns (text preserved verbatim)
COLUMN_MAP = {
    "Date":         "date_text",
    "Type":         "type",
    "Title":        "title",
    "Info":         "info",
    "Currency":     "currency",
    "Amount":       "amount_text",
    "Fees & Taxes": "fees_taxes_text",
    "Net":          "net_text",
    "Tax Details":  "tax_details",
}

INSERT_SQL = """
INSERT INTO raw.etsy_statements (
    source_file, line_number, statement_month,
    date_text, type, title, info, currency,
    amount_text, fees_taxes_text, net_text, tax_details,
    txn_date, amount, fees_taxes, net
) VALUES (
    %(source_file)s, %(line_number)s, %(statement_month)s,
    %(date_text)s, %(type)s, %(title)s, %(info)s, %(currency)s,
    %(amount_text)s, %(fees_taxes_text)s, %(net_text)s, %(tax_details)s,
    %(txn_date)s, %(amount)s, %(fees_taxes)s, %(net)s
)
ON CONFLICT (source_file, line_number) DO NOTHING
"""


def parse_statement_month(filename: str) -> date:
    """etsy_statement_2026_3.csv → date(2026, 3, 1)"""
    m = STATEMENT_FILENAME_RE.match(filename)
    if not m:
        raise ValueError(f"Filename does not match etsy_statement_YYYY_M.csv: {filename}")
    return date(int(m.group(1)), int(m.group(2)), 1)


def parse_money(s):
    """'$12.34' / '-$2.13' / '--' / '' / NaN → float or None"""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s == "--" or s.lower() == "nan":
        return None
    # Strip $ , spaces; keep sign
    cleaned = s.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_txn_date(s):
    """'March 31, 2026' → date(2026, 3, 31), tolerant of variations."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).strip()
    if not s or s == "--":
        return None
    try:
        return pd.to_datetime(s, errors="coerce").date()
    except Exception:
        return None


def load_statement(filepath: Path, cursor, dry_run: bool) -> int:
    statement_month = parse_statement_month(filepath.name)
    df = pd.read_csv(filepath, dtype=str, encoding="utf-8-sig")

    # Strip any BOM/whitespace from headers (utf-8-sig should already handle BOM)
    df.columns = [c.strip() for c in df.columns]

    # Warn on unexpected columns (schema drift signal)
    expected = set(COLUMN_MAP.keys())
    actual = set(df.columns)
    missing = expected - actual
    extra = actual - expected
    if missing:
        print(f"  WARN: missing expected columns in {filepath.name}: {missing}")
    if extra:
        print(f"  WARN: unexpected columns in {filepath.name}: {extra}")

    records = []
    for line_idx, row in df.iterrows():
        rec = {
            "source_file":     filepath.name,
            "line_number":     int(line_idx) + 1,  # 1-based, data row 1 = first non-header
            "statement_month": statement_month,
            "date_text":       row.get("Date"),
            "type":            row.get("Type"),
            "title":           row.get("Title"),
            "info":            row.get("Info"),
            "currency":        row.get("Currency"),
            "amount_text":     row.get("Amount"),
            "fees_taxes_text": row.get("Fees & Taxes"),
            "net_text":        row.get("Net"),
            "tax_details":     row.get("Tax Details"),
            "txn_date":        parse_txn_date(row.get("Date")),
            "amount":          parse_money(row.get("Amount")),
            "fees_taxes":      parse_money(row.get("Fees & Taxes")),
            "net":             parse_money(row.get("Net")),
        }
        # Convert pandas NaN to None for object fields
        for k, v in rec.items():
            if isinstance(v, float) and pd.isna(v):
                rec[k] = None
            elif v == "nan":
                rec[k] = None
        records.append(rec)

    if dry_run:
        print(f"  DRY RUN — {len(records)} rows would be inserted (statement_month={statement_month})")
        return len(records)

    cursor.executemany(INSERT_SQL, records)
    inserted = cursor.rowcount  # may be less than len(records) if conflicts
    return inserted


def process_file(filepath: Path, dry_run: bool = False):
    if not STATEMENT_FILENAME_RE.match(filepath.name):
        print(f"  SKIPPED (filename does not match etsy_statement_YYYY_M.csv): {filepath.name}")
        return

    print(f"Processing {filepath.name} → raw.etsy_statements...")
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            count = load_statement(filepath, cur, dry_run)
            if not dry_run:
                conn.commit()
                print(f"  Inserted {count} new rows (conflicts skipped).")


def parse_args():
    parser = argparse.ArgumentParser(description="Load Etsy monthly statement CSVs into raw.etsy_statements")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-dir", help="Directory to scan for etsy_statement_*.csv files")
    group.add_argument("--file", help="Load a single statement file")
    parser.add_argument("--dry-run", action="store_true", help="Parse files but do not insert")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.file:
        process_file(Path(args.file), dry_run=args.dry_run)
    else:
        source_dir = Path(args.source_dir)
        csv_files = sorted(source_dir.glob("etsy_statement_*.csv"))
        if not csv_files:
            print(f"No etsy_statement_*.csv files found in {source_dir}")
            return
        for f in csv_files:
            process_file(f, dry_run=args.dry_run)

    print("Done.")


if __name__ == "__main__":
    main()
