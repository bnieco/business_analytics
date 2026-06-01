"""
load_ga4.py — GA4 BigQuery export → business.raw.ga4_events

Per-event-date loader. For each date in the requested window:
  1. Look up the BQ table `events_YYYYMMDD`. Skip if absent (GA4 not yet
     published — common on the current day or first hours of the next).
  2. SELECT promoted scalars + nested arrays (as JSON strings) from BQ.
  3. In a single Postgres transaction:
       DELETE FROM raw.ga4_events WHERE event_date = <X>
       INSERT new batch
       UPSERT raw.ga4_load_state (increments load_count on conflict)
  4. Log a one-line summary and exit non-zero if any date failed.

The 3-day re-pull window means that on every run we re-load the last
N event_dates (default 3). GA4 backfills events into prior-day tables
for ~72h, so re-pulling is the simplest path to eventual consistency.

Designed for unattended launchd execution:
  * `logging` (no print() in the load path; print is reserved for --dry-run)
  * Per-date failure isolation: one bad day does not stop the run, but the
    process exits 1 so launchd can flag it.
  * No interactive prompts.

Usage:
  # Smoke-test connections, no data movement
  python scripts/load_ga4.py --dry-run

  # Default: re-pull last 3 complete days
  python scripts/load_ga4.py

  # Specific window
  python scripts/load_ga4.py --since 2026-05-15 --until 2026-05-21

  # Adjust the re-pull window (default 3 days)
  python scripts/load_ga4.py --backfill-days 7

  # Bump log level
  python scripts/load_ga4.py --verbose

Environment (.env):
  BUSINESS_DB_*       Postgres connection (see .env.example)
  GA4_BQ_PROJECT      GCP project hosting the GA4 export
  GA4_BQ_DATASET      BigQuery dataset (e.g. analytics_XXXXXXXXX)
  GA4_SA_KEY_PATH     Path to the service account JSON key
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.oauth2 import service_account
from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Allow `from db import get_engine` when running as a script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_engine  # noqa: E402


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GA4_BQ_PROJECT  = os.getenv("GA4_BQ_PROJECT")
GA4_BQ_DATASET  = os.getenv("GA4_BQ_DATASET")
GA4_SA_KEY_PATH = os.getenv("GA4_SA_KEY_PATH")

DEFAULT_BACKFILL_DAYS = 3

log = logging.getLogger("load_ga4")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def get_bq_client() -> bigquery.Client:
    """Build a BigQuery client from the configured service-account key."""
    missing = [v for v in ("GA4_BQ_PROJECT", "GA4_SA_KEY_PATH") if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"Missing required env var(s): {', '.join(missing)}")

    key_path = Path(GA4_SA_KEY_PATH)
    if not key_path.is_absolute():
        key_path = (PROJECT_ROOT / key_path).resolve()
    if not key_path.exists():
        raise FileNotFoundError(f"GA4 service-account key not found: {key_path}")

    creds = service_account.Credentials.from_service_account_file(str(key_path))
    return bigquery.Client(project=GA4_BQ_PROJECT, credentials=creds)


# ---------------------------------------------------------------------------
# Connection checks (used by --dry-run only; load path trusts the env)
# ---------------------------------------------------------------------------

def check_bq(client: bigquery.Client) -> None:
    """Confirm BQ creds + project + dataset are usable."""
    print(f"  BigQuery project : {client.project}")
    print(f"  BigQuery dataset : {GA4_BQ_DATASET}")

    row = next(client.query("SELECT 1 AS ok").result())
    print(f"  BigQuery probe   : SELECT 1 -> {row.ok}")

    if GA4_BQ_DATASET:
        dataset_ref = f"{client.project}.{GA4_BQ_DATASET}"
        tables = list(client.list_tables(dataset_ref, max_results=5))
        print(f"  Dataset tables   : {len(tables)} (showing up to 5)")
        for t in tables:
            print(f"    - {t.table_id}")


def check_postgres() -> None:
    """Confirm we can reach the business DB and the raw schema exists."""
    engine = get_engine()
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar()
        print(f"  Postgres         : {version.split(',')[0]}")

        has_schema = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'raw')"
        )).scalar()
        print(f"  raw schema       : {'present' if has_schema else 'MISSING (run migration 001)'}")

        for tbl in ("ga4_events", "ga4_load_state"):
            exists = conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='raw' AND table_name=:t)"
            ), {"t": tbl}).scalar()
            print(f"  raw.{tbl:<14} : {'present' if exists else 'MISSING (run migration 001)'}")


# ---------------------------------------------------------------------------
# Date-window planning
# ---------------------------------------------------------------------------

def plan_window(since: date | None, until: date | None, backfill_days: int) -> tuple[date, date]:
    """Return (start, end) event_dates to process."""
    today = date.today()
    if until is None:
        until = today - timedelta(days=1)
    if since is None:
        since = until - timedelta(days=backfill_days - 1)
    if since > until:
        raise ValueError(f"--since ({since}) is after --until ({until})")
    return since, until


# ---------------------------------------------------------------------------
# BigQuery extract
# ---------------------------------------------------------------------------

# Promoted scalars + nested arrays serialized to JSON strings. Listing ID and
# session metadata are pulled out of event_params in the source query so the
# Python side stays dumb.
EXTRACT_SQL_TEMPLATE = """
SELECT
    PARSE_DATE('%Y%m%d', event_date)        AS event_date,
    event_timestamp,
    event_name,
    event_bundle_sequence_id,

    user_pseudo_id,
    user_id,

    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id')     AS ga_session_id,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_number') AS ga_session_number,

    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location') AS page_location,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_title')    AS page_title,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_referrer') AS page_referrer,

    SAFE_CAST(
      REGEXP_EXTRACT(
        (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location'),
        r'/listing/(\\d+)/'
      ) AS INT64
    ) AS listing_id,

    device.category                AS device_category,
    device.mobile_brand_name       AS device_mobile_brand,
    device.mobile_model_name       AS device_mobile_model,
    device.operating_system        AS device_operating_system,
    device.web_info.browser        AS device_browser,
    device.language                AS device_language,

    geo.country                    AS geo_country,
    geo.region                     AS geo_region,
    geo.city                       AS geo_city,
    geo.continent                  AS geo_continent,

    traffic_source.name            AS traffic_source_name,
    traffic_source.medium          AS traffic_source_medium,
    traffic_source.source          AS traffic_source_source,

    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'source')   AS collected_traffic_source_source,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'medium')   AS collected_traffic_source_medium,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'campaign') AS collected_traffic_source_campaign,

    TO_JSON_STRING(event_params)    AS event_params_json,
    TO_JSON_STRING(items)           AS items_json,
    TO_JSON_STRING(user_properties) AS user_properties_json
FROM `{project}.{dataset}.{table}`
"""


@dataclass
class DateResult:
    event_date: date
    table_id: str
    status: str  # "loaded" | "skipped" | "failed"
    row_count: int = 0
    error: str | None = None


def _bq_table_exists(client: bigquery.Client, table_ref: str) -> bigquery.Table | None:
    """Return the BQ table object, or None if it does not exist."""
    try:
        return client.get_table(table_ref)
    except NotFound:
        return None


def _extract_date_from_bq(client: bigquery.Client, table_id: str) -> list[dict]:
    """Run the per-date extract query and return a list of row dicts ready
    for INSERT into raw.ga4_events. JSON string columns are parsed to Python
    objects so SQLAlchemy can serialize them to jsonb."""
    sql = EXTRACT_SQL_TEMPLATE.format(
        project=GA4_BQ_PROJECT, dataset=GA4_BQ_DATASET, table=table_id
    )
    log.debug("BQ extract SQL for %s:\n%s", table_id, sql)

    rows: list[dict] = []
    for r in client.query(sql).result():
        d = dict(r)
        # Parse JSON strings into Python objects for jsonb insertion.
        # TO_JSON_STRING returns the literal string "null" for NULL inputs;
        # json.loads("null") -> None, which is what we want.
        for json_col in ("event_params_json", "items_json", "user_properties_json"):
            raw = d.pop(json_col)
            target = json_col.removesuffix("_json")
            d[target] = json.loads(raw) if raw is not None else None
        rows.append(d)
    return rows


# ---------------------------------------------------------------------------
# Postgres load
# ---------------------------------------------------------------------------

INSERT_COLUMNS = [
    "event_date", "event_timestamp", "event_name", "event_bundle_sequence_id",
    "user_pseudo_id", "user_id", "ga_session_id", "ga_session_number",
    "page_location", "page_title", "page_referrer", "listing_id",
    "device_category", "device_mobile_brand", "device_mobile_model",
    "device_operating_system", "device_browser", "device_language",
    "geo_country", "geo_region", "geo_city", "geo_continent",
    "traffic_source_name", "traffic_source_medium", "traffic_source_source",
    "collected_traffic_source_source", "collected_traffic_source_medium",
    "collected_traffic_source_campaign",
    "event_params", "items", "user_properties",
    "bq_export_table",
]

# Transaction-scoped advisory lock keyed on the event_date string. Two
# concurrent loaders for the same date will queue on this lock; the lock is
# released automatically on commit/rollback. hashtext() converts the text key
# to the bigint that pg_advisory_xact_lock expects.
ADVISORY_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(hashtext('raw.ga4_events:load:' || :event_date_str))"
)

DELETE_SQL = text("DELETE FROM raw.ga4_events WHERE event_date = :event_date")

INSERT_SQL = text(
    "INSERT INTO raw.ga4_events ("
    + ", ".join(INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join(
        # event_params, items, user_properties go in as jsonb via explicit cast.
        f"CAST(:{c} AS JSONB)" if c in ("event_params", "items", "user_properties") else f":{c}"
        for c in INSERT_COLUMNS
    )
    + ")"
)

UPSERT_STATE_SQL = text(
    """
    INSERT INTO raw.ga4_load_state (event_date, bq_export_table, row_count,
                                    first_loaded_at, last_loaded_at, load_count)
    VALUES (:event_date, :bq_export_table, :row_count, now(), now(), 1)
    ON CONFLICT (event_date) DO UPDATE
       SET bq_export_table = EXCLUDED.bq_export_table,
           row_count       = EXCLUDED.row_count,
           last_loaded_at  = now(),
           load_count      = raw.ga4_load_state.load_count + 1
    """
)


def _normalize_row_for_insert(row: dict, bq_export_table: str) -> dict:
    """Add bq_export_table, ensure jsonb columns are JSON-encoded strings (psycopg2
    won't auto-serialize dicts/lists into TEXT-bound params; we cast to jsonb in
    SQL above)."""
    out = {col: row.get(col) for col in INSERT_COLUMNS}
    out["bq_export_table"] = bq_export_table
    for json_col in ("event_params", "items", "user_properties"):
        v = out[json_col]
        out[json_col] = json.dumps(v) if v is not None else None
    return out


def load_one_date(
    engine: Engine,
    bq_client: bigquery.Client,
    event_date: date,
) -> DateResult:
    """Pull one event_date from BQ and atomically replace it in Postgres."""
    table_id = f"events_{event_date.strftime('%Y%m%d')}"
    table_ref = f"{GA4_BQ_PROJECT}.{GA4_BQ_DATASET}.{table_id}"

    log.info("Processing event_date=%s (BQ table=%s)", event_date, table_id)

    bq_table = _bq_table_exists(bq_client, table_ref)
    if bq_table is None:
        log.warning(
            "BQ table %s not found — GA4 has not published this date yet; skipping",
            table_id,
        )
        return DateResult(event_date=event_date, table_id=table_id, status="skipped")

    try:
        rows = _extract_date_from_bq(bq_client, table_id)
    except Exception as e:
        log.exception("BQ extract failed for %s: %s", table_id, e)
        return DateResult(
            event_date=event_date, table_id=table_id, status="failed", error=str(e)
        )

    log.info("Extracted %d rows from BQ for %s", len(rows), table_id)

    insert_rows = [_normalize_row_for_insert(r, table_id) for r in rows]

    try:
        with engine.begin() as conn:
            # Serialize same-date loaders so two concurrent runs don't race
            # on the DELETE+INSERT. Transaction-scoped: auto-released on
            # commit/rollback.
            event_date_str = event_date.isoformat()
            lock_start = time.monotonic()
            conn.execute(ADVISORY_LOCK_SQL, {"event_date_str": event_date_str})
            lock_wait = time.monotonic() - lock_start
            log.info("Acquired advisory lock for event_date=%s", event_date_str)
            if lock_wait > 2.0:
                log.info(
                    "Advisory lock for event_date=%s took %.1fs to acquire — "
                    "another loader was likely in flight",
                    event_date_str, lock_wait,
                )

            deleted = conn.execute(DELETE_SQL, {"event_date": event_date}).rowcount
            log.info("Deleted %d existing rows for event_date=%s", deleted, event_date)

            if insert_rows:
                # SQLAlchemy will batch this; for the current per-day volume
                # (single digits to low thousands) one executemany is fine.
                conn.execute(INSERT_SQL, insert_rows)

            conn.execute(
                UPSERT_STATE_SQL,
                {
                    "event_date": event_date,
                    "bq_export_table": table_id,
                    "row_count": len(insert_rows),
                },
            )
    except Exception as e:
        log.exception("Postgres write failed for %s: %s", event_date, e)
        return DateResult(
            event_date=event_date, table_id=table_id, status="failed", error=str(e)
        )

    log.info("Loaded %d rows for event_date=%s", len(insert_rows), event_date)
    return DateResult(
        event_date=event_date,
        table_id=table_id,
        status="loaded",
        row_count=len(insert_rows),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_date_arg(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_args():
    p = argparse.ArgumentParser(description="Load GA4 BigQuery export into raw.ga4_events")
    p.add_argument("--since", type=parse_date_arg, help="Start event_date (YYYY-MM-DD, inclusive)")
    p.add_argument("--until", type=parse_date_arg, help="End event_date (YYYY-MM-DD, inclusive)")
    p.add_argument(
        "--backfill-days",
        type=int,
        default=DEFAULT_BACKFILL_DAYS,
        help=f"Re-pull window in days when --since is omitted (default: {DEFAULT_BACKFILL_DAYS})",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Verify connections and print plan; do not transfer data")
    p.add_argument("--verbose", action="store_true",
                   help="Set log level to DEBUG")
    return p.parse_args()


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main():
    args = parse_args()
    configure_logging(args.verbose)
    start, end = plan_window(args.since, args.until, args.backfill_days)

    if args.dry_run:
        # Dry-run preserves the original human-readable preview on stdout.
        print("=" * 70)
        print("load_ga4.py — DRY RUN (connection check + plan)")
        print("=" * 70)
        print(f"Window           : {start} -> {end}  ({(end - start).days + 1} day(s))")
        print(f"Backfill window  : {args.backfill_days} day(s)")
        print()

        print("Checking Postgres ...")
        check_postgres()
        print()

        print("Checking BigQuery ...")
        bq = get_bq_client()
        check_bq(bq)
        print()

        print("DRY RUN — would load these GA4 export tables:")
        d = start
        while d <= end:
            print(f"  events_{d.strftime('%Y%m%d')}  ->  raw.ga4_events  (event_date={d})")
            d += timedelta(days=1)
        print()
        print("No data transferred. Connections OK.")
        return 0

    # ---- Real load path -----------------------------------------------------
    log.info(
        "Starting GA4 load: window=%s -> %s (%d day(s)), backfill_days=%d",
        start, end, (end - start).days + 1, args.backfill_days,
    )

    engine = get_engine()
    bq_client = get_bq_client()

    results: list[DateResult] = []
    d = start
    while d <= end:
        results.append(load_one_date(engine, bq_client, d))
        d += timedelta(days=1)

    loaded = [r for r in results if r.status == "loaded"]
    skipped = [r for r in results if r.status == "skipped"]
    failed  = [r for r in results if r.status == "failed"]
    total_rows = sum(r.row_count for r in loaded)

    log.info(
        "LOAD SUMMARY: %d dates processed, %d succeeded, %d skipped, %d failed, %d total rows loaded",
        len(results), len(loaded), len(skipped), len(failed), total_rows,
    )
    for r in failed:
        log.error("Failed: event_date=%s table=%s err=%s", r.event_date, r.table_id, r.error)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
