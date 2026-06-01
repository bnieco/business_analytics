"""
enter_direct_order.py — interactive CLI for Direct order entry

Prompts for customer (match or create) + line items (existing parts by SKU)
and inserts a single transaction into core.customers / core.orders / core.order_items.

Usage:
    python scripts/enter_direct_order.py
"""
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2
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


def prompt(label: str, default: str | None = None, allow_blank: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{label}{suffix}: ").strip()
        if not val and default is not None:
            return default
        if val or allow_blank:
            return val


def prompt_decimal(label: str, default: str = "0") -> Decimal:
    while True:
        raw = prompt(label, default=default)
        try:
            return Decimal(raw)
        except Exception:
            print("  Not a valid number, try again.")


def prompt_int(label: str, default: str | None = None) -> int:
    while True:
        raw = prompt(label, default=default)
        try:
            return int(raw)
        except ValueError:
            print("  Not a valid integer, try again.")


def resolve_customer(cur) -> int:
    """Match existing direct customer by name/email or create a new one."""
    print("\n--- Customer ---")
    name = prompt("Customer display name")
    email = prompt("Email (blank if none)", allow_blank=True) or None
    phone = prompt("Phone (blank if none)", allow_blank=True) or None

    # Try to find an existing direct customer by email first, then by name
    if email:
        cur.execute(
            "SELECT customer_id, display_name FROM core.customers "
            "WHERE source = 'direct' AND lower(email) = lower(%s) AND deleted_at IS NULL",
            (email,),
        )
        row = cur.fetchone()
        if row:
            print(f"  Matched existing customer #{row[0]} ({row[1]}) by email.")
            return row[0]

    cur.execute(
        "SELECT customer_id, display_name, email FROM core.customers "
        "WHERE source = 'direct' AND lower(display_name) = lower(%s) AND deleted_at IS NULL",
        (name,),
    )
    matches = cur.fetchall()
    if matches:
        print("  Possible existing matches:")
        for i, (cid, dn, em) in enumerate(matches, 1):
            print(f"    {i}. #{cid} {dn} <{em or 'no email'}>")
        choice = prompt("Use existing? (number, or blank to create new)", allow_blank=True)
        if choice:
            return matches[int(choice) - 1][0]

    cur.execute(
        """
        INSERT INTO core.customers (source, display_name, email, phone, first_seen_at, last_order_at)
        VALUES ('direct', %s, %s, %s, now(), now())
        RETURNING customer_id
        """,
        (name, email, phone),
    )
    new_id = cur.fetchone()[0]
    print(f"  Created customer #{new_id}.")
    return new_id


def resolve_part(cur, sku: str):
    cur.execute(
        "SELECT part_id, name FROM core.parts WHERE sku = %s AND deleted_at IS NULL",
        (sku,),
    )
    return cur.fetchone()


def collect_line_items(cur) -> list[dict]:
    print("\n--- Line items ---")
    items: list[dict] = []
    while True:
        sku = prompt("Part SKU (blank to finish)", allow_blank=True)
        if not sku:
            if not items:
                print("  At least one line item is required.")
                continue
            break
        part = resolve_part(cur, sku)
        if not part:
            print(f"  No active part with SKU={sku}. Add it to core.parts first, then retry.")
            continue
        part_id, part_name = part
        print(f"  → {part_name} (part_id={part_id})")
        qty = prompt_int("  Quantity", default="1")
        unit_price = prompt_decimal("  Unit price (USD)")
        items.append({
            "part_id": part_id,
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": unit_price * qty,
        })
    return items


def main():
    print("=== Direct Order Entry ===")

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            customer_id = resolve_customer(cur)
            items = collect_line_items(cur)

            print("\n--- Order totals ---")
            subtotal = sum((i["line_total"] for i in items), Decimal(0))
            shipping = prompt_decimal("Shipping (USD)", default="0")
            tax = prompt_decimal("Tax (USD)", default="0")
            total = subtotal + shipping + tax
            ordered_at_raw = prompt(
                "Ordered at (ISO 8601, blank = now)",
                default=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            notes = prompt("Order notes (blank if none)", allow_blank=True) or None

            print(f"\nSubtotal: ${subtotal}  Shipping: ${shipping}  Tax: ${tax}  Total: ${total}")
            if prompt("Confirm insert? (y/N)", default="N").lower() != "y":
                print("Aborted.")
                return

            cur.execute(
                """
                INSERT INTO core.orders
                    (source, customer_id, ordered_at, status, subtotal, shipping, tax, total, notes)
                VALUES ('direct', %s, %s, 'open', %s, %s, %s, %s, %s)
                RETURNING order_id
                """,
                (customer_id, ordered_at_raw, subtotal, shipping, tax, total, notes),
            )
            order_id = cur.fetchone()[0]

            for it in items:
                cur.execute(
                    """
                    INSERT INTO core.order_items
                        (order_id, part_id, quantity, unit_price, line_total)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (order_id, it["part_id"], it["quantity"], it["unit_price"], it["line_total"]),
                )

            cur.execute(
                "UPDATE core.customers SET last_order_at = %s WHERE customer_id = %s",
                (ordered_at_raw, customer_id),
            )
            conn.commit()
            print(f"\nInserted order #{order_id} with {len(items)} line items.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
