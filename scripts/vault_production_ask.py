#!/usr/bin/env python
"""vault_production_ask.py -- file a vault production-block ASK into the vault Inbox.

Owner: Edison (Analytics Engineer).  Built 2026-09-14.

WHY THIS EXISTS
---------------
Brian's standing rule (banked 2026-09-14): every solid-wood dice vault order gets a
production block on the ENR Calendar -- 1 day mill, 1 day sand, 3 days finish, starting
the day AFTER the order comes in.

Asked how that should fire for future orders, Brian chose "schedule an inbox ask --
session starts are too rare."  He explicitly rejected both (a) Carson noticing it at
session start and (b) the pipeline auto-creating the calendar events.

So this job runs on launchd, with no Claude session alive, and its ENTIRE outward surface
is ONE markdown file written into the vault Inbox.  A human reads it, approves it, and the
calendar events are created by that human (or by Carson under his approval).

HARD BOUNDARY -- DO NOT WIDEN WITHOUT ASKING BRIAN
--------------------------------------------------
This script MUST NEVER:
  * write to Google Calendar (that was the option Brian declined),
  * call the Etsy API (the 06:15 loader owns that; this reads the warehouse only),
  * contact a customer, in any channel,
  * write anywhere in the vault except  AI Teams/Business/Inbox/ .
It reads core.* / ops.*, it writes ops.vault_production_ask_log, and it writes one
`ASK - *.md`.  That is the whole contract.

ORDER-DATE PRECISION -- THE 07:00 UTC ARTIFACT (read this before trusting a date)
---------------------------------------------------------------------------------
`core.orders.ordered_at` for every Etsy order reads exactly `YYYY-MM-DD 07:00:00+00`.
That is not a real purchase time.  Lineage:

  1. scripts/etsy_api.py:491  `_ts_to_date()` does
         datetime.fromtimestamp(created_timestamp, tz=timezone.utc).date()
     -- it throws away the clock and keeps the **UTC calendar date**.
  2. That bare `date` is inserted into `core.orders.ordered_at`, a `timestamptz`
     (scripts/load_core.py, the orders INSERT).  Postgres resolves a bare date against
     the session TimeZone, which on this server is `America/Phoenix` (verified 2026-09-14).
     Phoenix is UTC-7 year round, so midnight Phoenix == 07:00Z.  Hence the constant 07:00.

  Consequence: the stored date is the **UTC** date of the purchase, not the local one.
  Phoenix being UTC-7, any purchase made at or after 17:00 local rolls forward onto the
  NEXT UTC date.  So for a stored date D, the true local purchase date is either
  D (if bought 00:00-16:59 local) or D-1 (if bought 17:00-23:59 local).
  **The database cannot tell these apart.**  The clock was destroyed upstream at step 1;
  no amount of querying recovers it.  Anyone who claims otherwise is guessing.

  Corroborated live on 2026-09-14 for receipt 4174306330: Brian said the order came in
  Sunday 09-13; the DB said 09-14; listing 4332433907's quantity decremented 16 -> 15 in
  the 09-14 06:15 snapshot, proving the sale landed before 06:15 Monday.  Intersect that
  with "UTC date == 09-14" (i.e. at or after 17:00 Phoenix on 09-13) and the true purchase
  window pins to [09-13 17:00, 09-14 06:15) Phoenix.  Brian was right; the DB was showing
  the ingest artifact.

HOW THIS SCRIPT TREATS IT -- deliberate, not silent
---------------------------------------------------
Three options were on the table:
  (a) anchor on the stored date verbatim,
  (b) always subtract a day,
  (c) refuse to propose dates and make the human derive them.

Chose (a), anchor on the stored date, for two reasons:

  1. It is the only option that does not invent precision the warehouse does not have.
     (b) would be right for evening purchases and wrong for daytime ones, with no way to
     know which -- trading a known bias for a coin flip is not an improvement.
  2. It reproduces the block Brian personally approved.  On 2026-09-14, receipt
     4174306330 has stored date 09-14; anchor+1..+5 gives mill 09-15, sand 09-16,
     finish 09-17..09-19.  Those are exactly the three all-day events Brian approved and
     Carson wrote to the ENR Calendar that evening.  Note that Brian reached them by a
     different route -- he was told the true order date was 09-13, was offered 09-14
     (the literal rule), 09-15, or business-days-only, and **chose 09-15**, i.e. he
     overrode the literal rule by one day.  The two routes converge on the same dates.
     That convergence is the justification; it is not a coincidence worth relying on
     blindly, which is why every ASK states the anchor and invites the override.

The ASK therefore always prints BOTH the stored date and the true local-date interval
[D-1, D], says plainly which one the proposal is anchored on, and reminds the reader that
Brian has overridden the start date before.  Nothing is silently picked.

The ASK body states the interval in ONE line and moves on.  An advisory corroboration
signal from `ops.listing_attention` (day-over-day listing quantity, the same signal that
settled the 09-14 case) is computed and written to the RUN LOG only -- deliberately not
into the ASK.  Brian's instruction 2026-09-14: "stop blocking things on details you're
making important.  When a dice vault comes in, I want the schedule."  The ASK is the
schedule; the analysis lives in the log for whoever goes looking.

The real fix is upstream: stop truncating `created_timestamp` to a UTC date and carry the
epoch through to `core.orders.ordered_at`.  That is a loader change in `scripts/etsy_api.py`
and is NOT made here (D-BUSINESS-003 is open and this script touches none of it).

WATERMARK
---------
State lives in `ops.vault_production_ask_log`, one row per core order, NOT in a scalar
high-water mark and NOT in a state file.  Reasons, in order of weight:

  * A scalar watermark is wrong for this pipeline.  `run_etsy_api_load.sh` runs
    `etsy_api.py --backfill-days 7`, so an order can land in `core` days after its
    `ordered_at`.  A max(ordered_at) watermark would step past it and skip it forever.
    A per-order ledger cannot.
  * `ops` is readable by the `mcp_readonly` role, so Carson can answer "did this order
    already get an ASK?" from a session with a single MCP query, with no shell access.
    A JSON state file on local disk is invisible to him and dies with the repo checkout.
  * It is backed up with the database rather than depending on one Mac's filesystem.
  * `raw.etsy_api_load_state` is deliberately NOT reused: that table is the Etsy loader's
    own ingest cursor, and overloading it would couple two jobs that fail independently.

The filesystem is the second guard: before writing, the script globs `Inbox/` and
`Inbox/processed/` for an existing ASK carrying the same receipt id and skips if found.
So a crash between "file written" and "ledger row inserted" cannot produce a duplicate.

On first creation the ledger is SEEDED: every vault order already in `core.orders` with an
ordered date before today is inserted with status 'seeded'.  Installing this job must not
retro-file a dozen ASKs for vaults shipped months ago.

USAGE
-----
    venv/bin/python scripts/vault_production_ask.py --dry-run     # print, write nothing
    venv/bin/python scripts/vault_production_ask.py               # live
    venv/bin/python scripts/vault_production_ask.py --order-id 144 --dry-run
"""
from __future__ import annotations

import argparse
import glob
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_engine  # noqa: E402

# --------------------------------------------------------------------------- config

VAULT_INBOX = Path(
    "/Users/bnieco/Library/Mobile Documents/iCloud~md~obsidian/Documents/"
    "Obsidian Vault/AI Teams/Business/Inbox"
)

# Solid-wood dice vault SKUs -- the parts that actually consume a mill/sand/finish block.
#
# Derived, not invented: these are exactly the `DT-%` parts whose `ops.product_catalog`
# row carries materials = 'Wood' AND which have a canonical Knowledgebase product record
# at  AI Teams/Business/Knowledgebase/Products/<SKU>.md  (verified 2026-09-14).
# Deliberately EXCLUDED:
#   DT-3DP-VPR001  materials = '3D Printed'  -- printed, no mill/sand/finish cycle
#   DT-GRD-001     materials = NULL, no KB record -- unclassified, do not guess
#
# This is a hardcoded allowlist ON PURPOSE.  Inferring it at runtime from a nullable
# free-text `materials` column would silently enrol or drop products as listing data
# changes.  `check_allowlist_drift()` below warns when a new wood DT-% part appears that
# is not listed here, so drift surfaces without the job quietly deciding for itself.
VAULT_SKUS = ("DT-VLT-001", "DT-UTL-001", "DT-VPR-001")

# Brian's rule, banked 2026-09-14: 1 day mill, 1 day sand, 3 days finish, starting the
# day after the order comes in.  CALENDAR days, not business days -- he was offered a
# business-days-only variant on 2026-09-14 and did not take it.
MILL_OFFSET = 1
SAND_OFFSET = 2
FINISH_START_OFFSET = 3
FINISH_END_OFFSET = 5

STATE_TABLE = "ops.vault_production_ask_log"

# --------------------------------------------------------------------------- state

DDL = """
CREATE TABLE IF NOT EXISTS ops.vault_production_ask_log (
    order_id           bigint PRIMARY KEY REFERENCES core.orders(order_id),
    source_order_id    text        NOT NULL,
    part_id            bigint,
    sku                text,
    ordered_date       date        NOT NULL,
    status             text        NOT NULL CHECK (status IN ('seeded', 'filed')),
    ask_filename       text,
    mill_date          date,
    sand_date          date,
    finish_start_date  date,
    finish_end_date    date,
    filed_at           timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ops.vault_production_ask_log IS
  'Per-order ledger for scripts/vault_production_ask.py. One row per solid-wood dice '
  'vault order. status=seeded means the order predates the job and gets no ASK; '
  'status=filed means an ASK markdown file was written into the vault Inbox. '
  'Deliberately a ledger, not a scalar watermark: the Etsy loader backfills 7 days, so '
  'orders can land out of order. Owner: Edison.';
GRANT SELECT ON ops.vault_production_ask_log TO mcp_readonly;
"""


def state_table_exists(conn) -> bool:
    return bool(
        conn.execute(
            text("SELECT to_regclass('ops.vault_production_ask_log') IS NOT NULL")
        ).scalar()
    )


def ensure_state_table(conn, today: date) -> bool:
    """Create the ledger if absent and seed it with pre-existing vault orders.

    Returns True if it was created this call (i.e. this is the install run).
    """
    if state_table_exists(conn):
        return False

    print(f"[state] {STATE_TABLE} does not exist -- creating.")
    for stmt in filter(None, (s.strip() for s in DDL.split(";\n"))):
        conn.execute(text(stmt))

    seeded = conn.execute(
        text(
            """
            INSERT INTO ops.vault_production_ask_log
                (order_id, source_order_id, part_id, sku, ordered_date, status)
            SELECT DISTINCT ON (o.order_id)
                   o.order_id, o.source_order_id, oi.part_id, p.sku,
                   (o.ordered_at AT TIME ZONE 'America/Phoenix')::date, 'seeded'
              FROM core.orders o
              JOIN core.order_items oi ON oi.order_id = o.order_id
              JOIN core.parts p        ON p.part_id  = oi.part_id
             WHERE p.sku = ANY(:skus)
               AND (o.ordered_at AT TIME ZONE 'America/Phoenix')::date < :today
             ORDER BY o.order_id
            ON CONFLICT (order_id) DO NOTHING
            RETURNING order_id
            """
        ),
        {"skus": list(VAULT_SKUS), "today": today},
    ).fetchall()
    print(
        f"[state] seeded {len(seeded)} pre-existing vault order(s) as 'seeded' "
        f"(ordered before {today}); no ASK will be filed for them."
    )
    return True


# --------------------------------------------------------------------------- queries


def check_allowlist_drift(conn) -> None:
    """Warn if a new wood DT-% part exists that VAULT_SKUS does not cover."""
    rows = conn.execute(
        text(
            """
            SELECT sku, name, materials
              FROM ops.product_catalog
             WHERE sku LIKE 'DT-%'
               AND materials ILIKE '%wood%'
               AND NOT (sku = ANY(:skus))
            """
        ),
        {"skus": list(VAULT_SKUS)},
    ).mappings().all()
    for r in rows:
        print(
            f"[warn] allowlist drift: {r['sku']} ({r['name']}) has materials="
            f"{r['materials']!r} but is not in VAULT_SKUS. A human must decide whether it "
            f"takes a mill/sand/finish block; this job will NOT enrol it automatically."
        )


def fetch_candidates(conn, *, ledger_available: bool, lookback_days: int,
                     order_id: int | None, today: date):
    """Vault orders with no ledger row (or, with no ledger, inside the lookback window)."""
    sql = """
        SELECT o.order_id,
               o.source_order_id,
               (o.ordered_at AT TIME ZONE 'America/Phoenix')::date AS ordered_date,
               o.ordered_at,
               o.status        AS order_status,
               o.total,
               o.created_at    AS row_created_at,
               oi.order_item_id,
               oi.part_id,
               p.sku,
               p.name          AS part_name,
               oi.quantity,
               oi.unit_price,
               oi.line_total,
               oi.raw_title,
               oi.raw_listing_id
          FROM core.orders o
          JOIN core.order_items oi ON oi.order_id = o.order_id
          JOIN core.parts p        ON p.part_id  = oi.part_id
         WHERE p.sku = ANY(:skus)
    """
    params: dict = {"skus": list(VAULT_SKUS)}

    if order_id is not None:
        sql += " AND o.order_id = :order_id"
        params["order_id"] = order_id
    else:
        if ledger_available:
            sql += """
               AND NOT EXISTS (SELECT 1 FROM ops.vault_production_ask_log l
                                WHERE l.order_id = o.order_id)
            """
        sql += " AND (o.ordered_at AT TIME ZONE 'America/Phoenix')::date >= :floor"
        params["floor"] = today - timedelta(days=lookback_days)

    sql += " ORDER BY o.ordered_at, o.order_id, oi.order_item_id"
    return conn.execute(text(sql), params).mappings().all()


def quantity_corroboration(conn, listing_id, ordered_date: date) -> str:
    """Advisory only: did this listing's quantity drop in the ordered_date snapshot?

    `ops.listing_attention` snapshots each listing at the 06:15 cron. A decrement in the
    snapshot dated D means the sale landed before 06:15 on D, which -- combined with the
    UTC-date constraint -- pins the purchase to [D-1 17:00, D 06:15) Phoenix, i.e. the
    previous LOCAL evening. This is the signal that settled receipt 4174306330 on
    2026-09-14. It is a heuristic: a quantity change can also be Brian restocking or
    editing the listing.
    """
    if listing_id is None:
        return "no listing id on the line item, so no quantity signal is available."
    row = conn.execute(
        text(
            """
            WITH s AS (
                SELECT snapshot_date, quantity,
                       LAG(quantity) OVER (ORDER BY snapshot_date) AS prev_quantity
                  FROM ops.listing_attention
                 WHERE listing_id = :lid
            )
            SELECT snapshot_date, quantity, prev_quantity
              FROM s WHERE snapshot_date = :d
            """
        ),
        {"lid": int(listing_id), "d": ordered_date},
    ).mappings().first()

    if row is None:
        return (
            f"no `ops.listing_attention` snapshot for listing {listing_id} on "
            f"{ordered_date}, so the quantity signal cannot be evaluated."
        )
    if row["prev_quantity"] is None:
        return (
            f"the {ordered_date} snapshot for listing {listing_id} has no prior day to "
            f"compare against (quantity {row['quantity']}), so no signal."
        )
    delta = row["quantity"] - row["prev_quantity"]
    if delta < 0:
        return (
            f"listing {listing_id} quantity fell {row['prev_quantity']} to "
            f"{row['quantity']} in the {ordered_date} snapshot. That snapshot is taken at "
            f"the 06:15 cron, so the sale landed before 06:15 on {ordered_date}. "
            f"Combined with the UTC-date constraint, the purchase most likely happened on "
            f"the EVENING of {ordered_date - timedelta(days=1)} Phoenix time. Heuristic, "
            f"not proof (a restock or listing edit moves quantity too)."
        )
    return (
        f"listing {listing_id} quantity did not fall in the {ordered_date} snapshot "
        f"({row['prev_quantity']} to {row['quantity']}), so the quantity signal does not "
        f"corroborate a previous-evening purchase. Treat the stored date as the local date "
        f"unless Brian recalls otherwise."
    )


# --------------------------------------------------------------------------- ask body


def compute_block(anchor: date) -> dict:
    return {
        "mill": anchor + timedelta(days=MILL_OFFSET),
        "sand": anchor + timedelta(days=SAND_OFFSET),
        "finish_start": anchor + timedelta(days=FINISH_START_OFFSET),
        "finish_end": anchor + timedelta(days=FINISH_END_OFFSET),
    }


def dow(d: date) -> str:
    return d.strftime("%a %Y-%m-%d")


def existing_ask_for(receipt: str) -> str | None:
    for folder in (VAULT_INBOX, VAULT_INBOX / "processed"):
        hits = glob.glob(str(folder / f"ASK - * - vault-production-block-{receipt}.md"))
        if hits:
            return hits[0]
    return None


def render_ask(order, items, filed_on: date) -> tuple[str, str]:
    """Return (filename, markdown body).

    Deliberately short.  Brian's instruction, 2026-09-14: "when a dice vault comes in, I
    want the schedule.  We'll iterate on whether it's in stock later."  So the three dates
    ARE the deliverable.  Identifying context stays; gating questions do not.  Species,
    ship-by, and stock readiness were removed on purpose -- they are a later iteration, not
    a precondition for the schedule showing up.  Target: actionable in about ten seconds.
    Do not grow this back into a questionnaire.
    """
    receipt = order["source_order_id"]
    anchor = order["ordered_date"]
    blk = compute_block(anchor)
    filename = f"ASK - {filed_on.isoformat()} - vault-production-block-{receipt}.md"

    item_lines = "\n".join(
        f"- {it['raw_title']} (`{it['sku']}`, qty {it['quantity']}, listing "
        f"`{it['raw_listing_id']}`)"
        for it in items
    )

    body = f"""---
type: ask
date: {filed_on.isoformat()}
from: vault-production-ask (scheduled job, owner Edison)
to: Carson (Business)
status: open
priority: high
pickup: now
receipt: "{receipt}"
order_id: {order['order_id']}
ordered_date: {anchor.isoformat()}
---

# Vault production block for receipt `{receipt}`

| Stage | Date |
|---|---|
| Mill | {dow(blk['mill'])} |
| Sand | {dow(blk['sand'])} |
| Finish (3 days) | {dow(blk['finish_start'])} through {dow(blk['finish_end'])} |

Put these three on the ENR Calendar as all-day events.

**Order:** receipt `{receipt}`, `core.orders.order_id` `{order['order_id']}`, ordered `{anchor.isoformat()}`.

{item_lines}

**Calendar days, not business days**, so a block can run through a weekend.

**Brian has overridden a start date before** (receipt `4174306330`, 2026-09-14), so move
the anchor if you want it moved.

The stored order date is a UTC-date ingest artifact, so the true local order date is
`{(anchor - timedelta(days=1)).isoformat()}` or `{anchor.isoformat()}`; shift everything a day earlier if it was the former.

**Done means:** the three events exist on the ENR Calendar, then this ASK is `status: done`
with an `**Outcome:**` line and moved to `Inbox/processed/`.

---
*Filed by `scripts/vault_production_ask.py` (launchd `com.eldersnine.vault-production-ask`).
Ledger: `ops.vault_production_ask_log`. Owner: Edison. The job writes this file and nothing
else: no calendar write, no Etsy call, no customer contact.*
"""
    return filename, body


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ASK that WOULD be written; write nothing, anywhere.")
    ap.add_argument("--lookback-days", type=int, default=14,
                    help="safety floor on how far back to consider orders (default 14).")
    ap.add_argument("--order-id", type=int, default=None,
                    help="force a single core.orders.order_id (ignores the ledger; "
                         "still refuses to duplicate an existing ASK file).")
    ap.add_argument("--limit", type=int, default=10,
                    help="max ASKs to file in one run (default 10; a blown-out run is a "
                         "bug, not a workload).")
    args = ap.parse_args()

    today = date.today()
    started = datetime.now()
    print(f"=== vault_production_ask start {started:%Y-%m-%d %H:%M:%S} "
          f"({'DRY RUN' if args.dry_run else 'LIVE'}) ===")

    if not args.dry_run and not VAULT_INBOX.is_dir():
        print(f"FATAL: vault Inbox not found at {VAULT_INBOX}", file=sys.stderr)
        return 1

    engine = get_engine()
    with engine.begin() as conn:
        check_allowlist_drift(conn)

        if args.dry_run:
            ledger_available = state_table_exists(conn)
            if not ledger_available:
                print(f"[dry-run] {STATE_TABLE} does not exist yet and a dry run will not "
                      f"create it. Falling back to the {args.lookback_days}-day window "
                      f"with an empty ledger, so this prints what a ledger-less run would "
                      f"consider. The first LIVE run creates the table and seeds every "
                      f"vault order dated before today as 'seeded', filing nothing for "
                      f"them.")
        else:
            created = ensure_state_table(conn, today)
            ledger_available = True
            if created:
                print("[state] install run complete. Orders from today forward will file.")

        rows = fetch_candidates(
            conn,
            ledger_available=ledger_available,
            lookback_days=args.lookback_days,
            order_id=args.order_id,
            today=today,
        )

        # group line items by order
        orders: dict[int, dict] = {}
        for r in rows:
            o = orders.setdefault(r["order_id"], {"order": r, "items": []})
            o["items"].append(r)

        if not orders:
            print("No new vault orders. Nothing to file.")
            print(f"=== vault_production_ask end {datetime.now():%Y-%m-%d %H:%M:%S} ===")
            return 0

        print(f"{len(orders)} candidate vault order(s): "
              f"{', '.join(str(k) for k in orders)}")

        filed = 0
        for oid, bundle in list(orders.items())[: args.limit]:
            order, items = bundle["order"], bundle["items"]
            receipt = order["source_order_id"]

            dupe = existing_ask_for(receipt)
            if dupe:
                print(f"[skip] order {oid} receipt {receipt}: an ASK already exists at "
                      f"{dupe}. Not re-filing.")
                continue

            # Advisory only, and LOG-ONLY by design. Brian does not want this in the
            # ASK body (2026-09-14): the ASK is the schedule, not an analysis.
            print(f"[date-signal] order {oid}: "
                  f"{quantity_corroboration(conn, order['raw_listing_id'], order['ordered_date'])}")
            filename, body = render_ask(order, items, today)
            target = VAULT_INBOX / filename

            if args.dry_run:
                print("\n" + "=" * 78)
                print(f"WOULD WRITE: {target}")
                print("=" * 78)
                print(body)
                print("=" * 78 + "\n")
                continue

            target.write_text(body, encoding="utf-8")
            # verify the file actually landed before claiming the order handled
            if not target.is_file() or target.stat().st_size == 0:
                print(f"FATAL: wrote {target} but it is missing or empty; leaving the "
                      f"ledger untouched so the next run retries.", file=sys.stderr)
                return 1
            print(f"[filed] {target} ({target.stat().st_size} bytes)")

            blk = compute_block(order["ordered_date"])
            conn.execute(
                text(
                    """
                    INSERT INTO ops.vault_production_ask_log
                        (order_id, source_order_id, part_id, sku, ordered_date, status,
                         ask_filename, mill_date, sand_date, finish_start_date,
                         finish_end_date, filed_at)
                    VALUES (:order_id, :receipt, :part_id, :sku, :ordered_date, 'filed',
                            :ask_filename, :mill, :sand, :fstart, :fend, now())
                    ON CONFLICT (order_id) DO UPDATE
                        SET status = 'filed', ask_filename = EXCLUDED.ask_filename,
                            filed_at = now()
                    """
                ),
                {
                    "order_id": oid,
                    "receipt": receipt,
                    "part_id": order["part_id"],
                    "sku": order["sku"],
                    "ordered_date": order["ordered_date"],
                    "ask_filename": filename,
                    "mill": blk["mill"],
                    "sand": blk["sand"],
                    "fstart": blk["finish_start"],
                    "fend": blk["finish_end"],
                },
            )
            filed += 1

        if not args.dry_run:
            print(f"Filed {filed} ASK(s).")

    print(f"=== vault_production_ask end {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
