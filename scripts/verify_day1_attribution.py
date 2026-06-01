"""verify_day1_attribution.py — one-command Day 1 attribution check.

Pre-staged 2026-05-24 (round 3, Edison). Designed to be run on 2026-05-26 or
2026-05-27 once GA4 → BigQuery → raw.ga4_events has caught up with Brian's
Day 1 (2026-05-24) Phase 1 reactivation traffic.

What it does
------------
Runs the diagnostic logic from scripts/queries/verify_day1_attribution.sql,
focused on the Fear Tracker (listing 4364365992) FB → Etsy click path Brian
manually verified in-browser on 2026-05-24. Summarizes the answer in plain
English so the verification step is one command, not a context reload.

Decision rule (matches the SQL file's WHAT TO LOOK FOR block):
  - If GA4 captured at least one Day 1 visit to listing 4364365992 carrying
    ep_content = 'fearTracker_hero_day1' → Convention CONFIRMED. The
    round-2 browser test matrix replicates in production GA4 data, so
    per-post utm_content attribution for Etsy URLs works end-to-end for at
    least the FB-referred listing-click path. The Brief Enrichment
    Protocol's UTM convention locks.
  - If GA4 captured visits to the listing but no ep_content matched →
    Convention CONTRADICTED. Brian's browser snapshot doesn't represent
    what GA4 actually ingests. Surfaces what GA4 saw instead so Brian can
    decide next steps.
  - If GA4 captured zero visits to the listing on Day 1 → INSUFFICIENT
    DATA. Either traffic was zero, or BQ export hasn't completed yet for
    Day 1. Re-run tomorrow.

Usage
-----
    ./venv/bin/python scripts/verify_day1_attribution.py

No arguments. Constants below are pinned to Brian's Day 1 hero post:
target_date = 2026-05-24, target_listing = 4364365992 (Fear Tracker),
expected utm_content = 'fearTracker_hero_day1'.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from db import query

TARGET_DATE = date(2026, 5, 24)
TARGET_LISTING = "4364365992"
EXPECTED_UTM_CONTENT = "fearTracker_hero_day1"


COUNTS_SQL = """
SELECT
    COUNT(*)                                                                          AS total_events,
    COUNT(*) FILTER (WHERE traffic_source_medium = 'referral')                        AS referral_events,
    COUNT(*) FILTER (WHERE traffic_source_source ~* 'facebook')                       AS facebook_referrer_events,
    COUNT(*) FILTER (WHERE collected_traffic_source_campaign = 'phase1_reactivation') AS phase1_campaign_events
FROM raw.ga4_events
WHERE event_date = :target_date
  AND event_name = 'page_view'
  AND page_location ~ ('/listing/' || :target_listing)
"""


# Per-visit detail. Mirrors the SQL file's "what GA4 actually captured" block.
DETAIL_SQL = """
WITH listing_visits AS (
    SELECT
        e.event_timestamp,
        e.user_pseudo_id,
        e.traffic_source_source,
        e.traffic_source_medium,
        e.collected_traffic_source_source,
        e.collected_traffic_source_campaign,
        e.page_location,
        e.event_params
    FROM raw.ga4_events e
    WHERE e.event_date = :target_date
      AND e.event_name = 'page_view'
      AND e.page_location ~ ('/listing/' || :target_listing)
)
SELECT
    to_timestamp(event_timestamp / 1000000)::timestamptz AT TIME ZONE 'America/Los_Angeles' AS visit_pt,
    LEFT(user_pseudo_id, 12)                AS pseudo_id_prefix,
    traffic_source_source                   AS ga_source,
    traffic_source_medium                   AS ga_medium,
    collected_traffic_source_source         AS coll_source,
    collected_traffic_source_campaign       AS coll_campaign,
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'source'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'source')
    )                                       AS ep_source,
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'campaign'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'campaign')
    )                                       AS ep_campaign,
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content')
    )                                       AS ep_content
FROM listing_visits
ORDER BY event_timestamp
"""


# Does any Day 1 visit carry the expected utm_content?
MATCH_SQL = """
SELECT
    COUNT(*)                          AS visits_with_expected_content,
    COUNT(DISTINCT user_pseudo_id)    AS unique_visitors_with_expected_content
FROM raw.ga4_events e
WHERE e.event_date = :target_date
  AND e.event_name = 'page_view'
  AND e.page_location ~ ('/listing/' || :target_listing)
  AND COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content')
      ) = :expected_content
"""


# Cross-check: did the LANDING URL carry utm_content even if GA4 didn't
# extract it into event_params? Distinguishes "URL was clean" from "GA4
# config didn't ingest it".
URL_QS_SQL = """
SELECT
    COUNT(*)                                                                   AS visits_with_qs_utm_content,
    COUNT(*) FILTER (WHERE page_location ~ ('utm_content=' || :expected_content)) AS visits_with_exact_qs_match
FROM raw.ga4_events
WHERE event_date = :target_date
  AND event_name = 'page_view'
  AND page_location ~ ('/listing/' || :target_listing)
  AND page_location ~ 'utm_content='
"""


def _banner(title: str) -> None:
    bar = "=" * (len(title) + 8)
    print(f"\n{bar}\n=== {title} ===\n{bar}")


def _print_df(df: pd.DataFrame) -> None:
    if df.empty:
        print("(no rows)")
        return
    print(df.to_string(index=False))


def _verdict(
    counts: pd.DataFrame,
    match: pd.DataFrame,
    url_qs: pd.DataFrame,
) -> str:
    """Return one of: CONFIRMED, CONTRADICTED, INSUFFICIENT_DATA."""
    total = int(counts.iloc[0]["total_events"]) if not counts.empty else 0
    matched = int(match.iloc[0]["visits_with_expected_content"]) if not match.empty else 0
    qs_present = int(url_qs.iloc[0]["visits_with_qs_utm_content"]) if not url_qs.empty else 0

    if total == 0:
        return "INSUFFICIENT_DATA"
    if matched > 0:
        return "CONFIRMED"
    # We saw visits to the listing but none carried the expected
    # utm_content. Distinguish "URL carried it but GA4 didn't ingest" from
    # "URL didn't carry it at all" in the printed summary below.
    return "CONTRADICTED"


def main() -> int:
    params = {
        "target_date": TARGET_DATE,
        "target_listing": TARGET_LISTING,
        "expected_content": EXPECTED_UTM_CONTENT,
    }

    print(
        f"\nVerifying Day 1 attribution:\n"
        f"  target_date     = {TARGET_DATE.isoformat()}\n"
        f"  target_listing  = {TARGET_LISTING} (Fear Tracker)\n"
        f"  expected_utm_content = {EXPECTED_UTM_CONTENT}"
    )

    counts = query(COUNTS_SQL, **params)
    _banner("Row counts: Day 1 listing 4364365992 page_views")
    _print_df(counts)

    detail = query(DETAIL_SQL, **params)
    _banner("Per-visit detail (what GA4 actually captured)")
    _print_df(detail)

    match = query(MATCH_SQL, **params)
    _banner(f"Visits matching ep_content = '{EXPECTED_UTM_CONTENT}'")
    _print_df(match)

    url_qs = query(URL_QS_SQL, **params)
    _banner("Query-string presence of utm_content in page_location")
    _print_df(url_qs)

    verdict = _verdict(counts, match, url_qs)

    _banner("VERDICT")
    if verdict == "CONFIRMED":
        matched = int(match.iloc[0]["visits_with_expected_content"])
        uniq = int(match.iloc[0]["unique_visitors_with_expected_content"])
        print(
            f"Convention CONFIRMED.\n\n"
            f"  GA4 captured {matched} Day 1 visit(s) to listing {TARGET_LISTING} "
            f"carrying utm_content = '{EXPECTED_UTM_CONTENT}' "
            f"({uniq} unique visitor(s)).\n"
            f"  The 6-cell browser test matrix replicates in production GA4 data\n"
            f"  for at least the FB → Etsy listing-click path. Per-post\n"
            f"  utm_content attribution for Etsy URLs is viable end-to-end.\n"
            f"  Brief Enrichment Protocol UTM convention locks."
        )
    elif verdict == "CONTRADICTED":
        total = int(counts.iloc[0]["total_events"])
        qs_present = int(url_qs.iloc[0]["visits_with_qs_utm_content"])
        qs_exact = int(url_qs.iloc[0]["visits_with_exact_qs_match"])
        print(
            f"Convention CONTRADICTED.\n\n"
            f"  GA4 saw {total} Day 1 visit(s) to listing {TARGET_LISTING} but\n"
            f"  zero carried ep_content = '{EXPECTED_UTM_CONTENT}'.\n"
            f"  page_location utm_content present in {qs_present} visit(s); "
            f"exact match in {qs_exact}.\n"
            f"  - If qs_present > 0: URL carried UTMs but GA4 didn't ingest →\n"
            f"    likely an Etsy GA4 property config issue, not URL stripping.\n"
            f"  - If qs_present = 0: URL didn't carry utm_content for these\n"
            f"    visits → unexpected vs Brian's 5/24 browser matrix. Flag for Brian."
        )
    else:  # INSUFFICIENT_DATA
        print(
            f"INSUFFICIENT DATA.\n\n"
            f"  Zero Day 1 page_view events for listing {TARGET_LISTING} are\n"
            f"  in raw.ga4_events yet. Either traffic was zero OR the BQ export\n"
            f"  for event_date = {TARGET_DATE.isoformat()} hasn't landed.\n"
            f"  Re-run tomorrow; if still zero by 5/28, check the GA4 → BQ\n"
            f"  pipeline and confirm the FB post drove any clicks at all."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
