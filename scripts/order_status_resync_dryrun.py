"""
order_status_resync_dryrun.py: READ-ONLY preview of the order status re-sync.

For every Etsy order still 'open' in core.orders, GETs the live receipt from
the Etsy v3 API (getShopReceipt, scope transactions_r) and prints what the
re-sync in load_core.py WOULD change (status, fulfilled_at). Writes nothing to
Postgres.

Token handling is the loader's own: etsy_api.refresh_access_token() is used,
which persists a rotated refresh token to .env exactly as the daily load does.
Run ONLY on the jobs host (M6); never on the MacBook (token rotation).

Usage:
  venv/bin/python scripts/order_status_resync_dryrun.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

from db import get_engine  # noqa: E402
import etsy_api  # noqa: E402
from load_core import derive_status  # noqa: E402


def main() -> int:
    engine = get_engine()
    with engine.connect() as conn:
        open_rows = conn.execute(text(
            "SELECT order_id, source_order_id, status, fulfilled_at FROM core.orders "
            "WHERE source = 'etsy' AND status = 'open' ORDER BY order_id"
        )).mappings().all()

    client = etsy_api.EtsyClient(etsy_api.refresh_access_token())
    shop_id = etsy_api.resolve_shop_id(client)
    loaded_at = datetime.now(tz=timezone.utc)

    print(f"{'order_id':>8}  {'receipt':>10}  {'old':<6} {'new':<10} {'fulfilled_at':<12} "
          f"{'etsy_status':<12} is_shipped")
    counts: dict[str, int] = {}
    for o in open_rows:
        receipt = client.get(f"/shops/{shop_id}/receipts/{o['source_order_id']}")
        raw = etsy_api.receipt_to_order_row(receipt, loaded_at)
        new_status, fulfilled_at = derive_status(raw)
        counts[new_status] = counts.get(new_status, 0) + 1
        print(f"{o['order_id']:>8}  {o['source_order_id']:>10}  {o['status']:<6} {new_status:<10} "
              f"{str(fulfilled_at):<12} {str(raw['status']):<12} {raw['is_shipped']}")

    print(f"\n{len(open_rows)} open in core.orders -> " +
          ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print("DRY RUN: nothing written to Postgres.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
