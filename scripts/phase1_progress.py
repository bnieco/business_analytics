"""phase1_progress.py — Phase 1 Reactivation Pilot weekly progress CLI.

CANONICAL query logic lives in scripts/dashboards/phase1_page.py (the
dashboard). This CLI and scripts/queries/*.sql MIRROR it. As of the
2026-06-01 consolidation the per-link query here uses the same rich
two-arm (etsy/own_site) attribution with the Flag-A-fixed shared
utm_content extraction as the dashboard, so CLI == dashboard == psql.

Runs the same queries as `scripts/queries/phase1_progress.sql` and
`scripts/queries/phase1_link_performance.sql` and formats them for terminal
reading. The SQL files use psql-only backslash directives (\\set / \\echo)
so this wrapper duplicates the constants in Python and runs each SELECT
separately via SQLAlchemy / pandas — keeping parity with `db.py` usage
elsewhere in the project.

Usage:
    ./venv/bin/python scripts/phase1_progress.py

The window constants below must stay in sync with the SQL files.

ATTRIBUTION NOTE (rewritten 2026-05-27, round 4, Edison):
    Round-3 (5/24) attribution was REFERRER-FLOOR: filter
    `traffic_source_medium = 'referral' AND traffic_source_source ~
    <hostname regex>` on raw.ga4_events. The 5/24 design assumption was
    that social→Etsy clicks would land in GA4 with referer-based
    attribution (hostname source values like 'facebook.com' + medium =
    'referral').

    Production data verified 2026-05-27 contradicted that assumption.
    UTM-tagged Phase 1 traffic lands in GA4 with LITERAL utm_* values:
        traffic_source_source = 'facebook'   (NOT 'facebook.com')
        traffic_source_medium = 'organic'    (NOT 'referral')
    GA4 takes utm_source / utm_medium verbatim from the URL query string.
    The round-3 filter matched zero Phase 1 events; the first formal Day 7
    weekly review (Sat 2026-05-30) would have reported zero.

    Round-4 fix: anchor the Phase 1 filter on the canonical CAMPAIGN TAG
    that scripts/utm.py stamps on every Phase 1 link:
        collected_traffic_source_campaign = 'phase1_reactivation'
    Filtering on campaign instead of platform-referrer:
      - catches every Phase 1 UTM-tagged event regardless of platform path
      - naturally EXCLUDES phase1_diag test-matrix events (Brian's 5/24
        6-cell browser test carries `phase1_diag`, not phase1_reactivation)
      - naturally EXCLUDES the Day 2 inthalo Meta-Ads paid click
        (campaign = `inthalo`, not phase1_reactivation)
      - naturally EXCLUDES (direct)/(none) organic non-pilot traffic

    Per-post (utm_content) attribution lives in
    phase1_link_performance.sql, which now joins raw.utm_links to
    raw.ga4_events on the literal collected_traffic_source_* columns
    (matching what's actually in production).

    Operational lesson (kept here so future-Edison reads it before
    touching attribution again): round-3 was encoded into production SQL
    on the strength of the 6-cell browser test matrix without verifying
    against actual GA4-ingested production data. The matrix confirmed
    UTMs survived URL transport end-to-end; it did NOT confirm what GA4
    wrote into traffic_source_* vs collected_traffic_source_* after
    ingestion. Verify against the downstream artifact you actually filter
    on, not the upstream artifact that feeds it. See Edison persona's
    "Verify before encoding" principle and feedback_verify_before_act.md.

    Basis: 2026-05-27 query of raw.ga4_events covering 2026-05-24 →
    2026-05-25. Distinct collected_traffic_source_campaign values in the
    window: NULL (direct/none), phase1_diag (test), phase1_reactivation
    (Day 1 FB click — what this dashboard now correctly surfaces), and
    inthalo (paid Meta Ads, correctly excluded).
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from db import query

# Window — keep in sync with scripts/queries/phase1_progress.sql
PHASE1_START = date(2026, 5, 24)
PHASE1_END = date(2026, 8, 22)
PHASE1_TARGET_VISITS = 500

# Canonical Phase 1 campaign tag — utm.py stamps this on every generated
# link via DEFAULT_CAMPAIGN. Keep in sync with scripts/utm.py and
# scripts/queries/phase1_progress.sql.
PHASE1_CAMPAIGN_TAG = "phase1_reactivation"


HEADLINE_SQL = """
WITH visits AS (
    SELECT COUNT(*) AS phase1_visits
    FROM raw.ga4_events
    WHERE event_date BETWEEN :start AND :end
      AND event_name = 'page_view'
      AND collected_traffic_source_campaign = :campaign_tag
)
SELECT
    DATE :start                                                  AS phase1_start,
    DATE :end                                                    AS phase1_end,
    CURRENT_DATE                                                 AS as_of,
    GREATEST(0, (DATE :end - CURRENT_DATE))                      AS days_remaining,
    LEAST(CURRENT_DATE, DATE :end) - DATE :start + 1             AS days_elapsed,
    v.phase1_visits                                              AS visits_to_date,
    :target                                                      AS visits_floor,
    ROUND(100.0 * v.phase1_visits / :target, 1)                  AS pct_of_floor,
    CASE
        WHEN (LEAST(CURRENT_DATE, DATE :end) - DATE :start + 1) > 0
        THEN ROUND(
            v.phase1_visits::numeric
            * 90.0
            / (LEAST(CURRENT_DATE, DATE :end) - DATE :start + 1)
        )
        ELSE NULL
    END                                                          AS pace_projection_90d
FROM visits v
"""

# By-platform view. collected_traffic_source_source carries the literal
# utm_source short name (e.g. 'facebook') rather than a hostname-style
# referrer ('facebook.com'). For Phase 1 UTM-tagged traffic this is the
# reliable per-platform discriminator.
BY_PLATFORM_SQL = """
SELECT
    COALESCE(collected_traffic_source_source, '(unset)')         AS platform,
    COUNT(*)                                                     AS visits,
    COUNT(DISTINCT user_pseudo_id)                               AS unique_visitors,
    COUNT(DISTINCT ga_session_id)                                AS sessions,
    MIN(event_date)                                              AS first_seen,
    MAX(event_date)                                              AS last_seen
FROM raw.ga4_events
WHERE event_date BETWEEN :start AND :end
  AND event_name = 'page_view'
  AND collected_traffic_source_campaign = :campaign_tag
GROUP BY 1
ORDER BY visits DESC
"""

# Per-landing-page view — groups Phase 1 visits by (platform, page_location).
# Per-post (utm_content) discrimination lives in phase1_link_performance.sql.
BY_LANDING_SQL = """
SELECT
    COALESCE(collected_traffic_source_source, '(unset)')         AS platform,
    regexp_replace(page_location, '\\?.*$', '')                  AS landing_page,
    COUNT(*)                                                     AS visits,
    COUNT(DISTINCT user_pseudo_id)                               AS unique_visitors,
    MIN(event_date)                                              AS first_seen,
    MAX(event_date)                                              AS last_seen
FROM raw.ga4_events
WHERE event_date BETWEEN :start AND :end
  AND event_name = 'page_view'
  AND collected_traffic_source_campaign = :campaign_tag
GROUP BY 1, 2
ORDER BY visits DESC
LIMIT 25
"""

ETSY_YOY_SQL = """
SELECT
    year,
    sub_source,
    visits,
    date_range_start,
    date_range_end
FROM raw.etsy_traffic_stats
WHERE category = 'Social Media'
ORDER BY year DESC, visits DESC NULLS LAST, sub_source NULLS FIRST
"""

# Per-link performance — joins raw.utm_links to GA4 events. Round-4
# (2026-05-27) rewrite: the Etsy attribution branch now matches against
# collected_traffic_source_source/campaign (literal utm values GA4 ingested)
# rather than the prior round-3 traffic_source_medium='referral' +
# hostname-regex filter, which matched zero production rows. Own-site branch
# was already correct (uses collected_traffic_source_*) and is unchanged.
# utm_content is still drawn from event_params (no collected_traffic_source_content
# column exists in raw.ga4_events). See the SQL file's ATTRIBUTION NOTE.
# Flag-A fix (2026-06-01): single shared utm_content extraction, anchored on
# the known-good nested form first, used identically in both arms — mirrors
# scripts/dashboards/phase1_page.py::_UTM_CONTENT_EXPR.
_UTM_CONTENT_EXPR = """COALESCE(
             (SELECT ep -> 'value' ->> 'string_value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1),
             (SELECT ep ->> 'value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1)
         )"""

LINK_PERFORMANCE_SQL = f"""
WITH
links AS (
    SELECT
        l.*,
        CASE
            WHEN l.base_url ~* '(^|//|\\.)etsy\\.com(/|\\?|$)' THEN 'etsy'
            ELSE 'own_site'
        END AS destination_kind
    FROM raw.utm_links l
    WHERE l.utm_campaign = :campaign_tag
),
etsy_attribution AS (
    -- Match against the literal utm values GA4 ingested
    -- (collected_traffic_source_source/campaign). utm_content discrimination
    -- comes from event_params since GA4's raw export has no
    -- collected_traffic_source_content column. Listing-id guard preserves
    -- the original sanity check.
    SELECT
        l.id AS link_id,
        COUNT(*)                       AS visits,
        COUNT(DISTINCT e.user_pseudo_id) AS unique_visitors,
        MIN(e.event_date)              AS first_visit,
        MAX(e.event_date)              AS last_visit
    FROM links l
    JOIN raw.ga4_events e
      ON l.destination_kind = 'etsy'
     AND e.event_name = 'page_view'
     AND e.event_date BETWEEN :start AND :end
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND (
         l.base_url !~ '/listing/[0-9]+'
         OR e.page_location ~ ('/listing/' || substring(l.base_url FROM '/listing/([0-9]+)'))
     )
     AND {_UTM_CONTENT_EXPR} IS NOT DISTINCT FROM l.utm_content
    GROUP BY l.id
),
own_site_attribution AS (
    SELECT
        l.id AS link_id,
        COUNT(*)                       AS visits,
        COUNT(DISTINCT e.user_pseudo_id) AS unique_visitors,
        MIN(e.event_date)              AS first_visit,
        MAX(e.event_date)              AS last_visit
    FROM links l
    JOIN raw.ga4_events e
      ON l.destination_kind = 'own_site'
     AND e.event_name = 'page_view'
     AND e.event_date BETWEEN :start AND :end
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND {_UTM_CONTENT_EXPR} IS NOT DISTINCT FROM l.utm_content
    GROUP BY l.id
),
attribution AS (
    SELECT * FROM etsy_attribution
    UNION ALL
    SELECT * FROM own_site_attribution
)
SELECT
    l.destination_kind,
    l.utm_source,
    l.utm_content,
    l.base_url,
    l.created_at::date              AS link_created,
    l.generated_count,
    COALESCE(a.visits, 0)           AS visits,
    COALESCE(a.unique_visitors, 0)  AS unique_visitors,
    a.first_visit,
    a.last_visit,
    'per_post'                      AS attribution_mode
FROM links l
LEFT JOIN attribution a ON a.link_id = l.id
ORDER BY visits DESC, l.created_at DESC
"""


def _print_section(title: str, df: pd.DataFrame) -> None:
    bar = "=" * (len(title) + 8)
    print(f"\n{bar}\n=== {title} ===\n{bar}")
    if df.empty:
        print("(no rows)")
        return
    print(df.to_string(index=False))


def _print_headline_summary(df: pd.DataFrame) -> None:
    """Plain-English banner above the headline table so the answer to
    'how is the pilot going' is the first thing on screen."""
    if df.empty:
        return
    row = df.iloc[0]
    print(
        f"\nPhase 1 ({row['phase1_start']} → {row['phase1_end']}) — "
        f"day {row['days_elapsed']} of 90, {row['days_remaining']} remaining.\n"
        f"  Visits attributed to the phase1_reactivation campaign tag: "
        f"{row['visits_to_date']} / {row['visits_floor']} "
        f"({row['pct_of_floor']}% of criterion-4 floor).\n"
        f"  At current pace, 90-day projection: "
        f"{row['pace_projection_90d']} visits."
    )


def main() -> int:
    params = {
        "start": PHASE1_START,
        "end": PHASE1_END,
        "target": PHASE1_TARGET_VISITS,
        "campaign_tag": PHASE1_CAMPAIGN_TAG,
    }

    headline = query(HEADLINE_SQL, **params)
    _print_headline_summary(headline)
    _print_section(
        "Phase 1 headline (criterion 4: 500-visit organic floor)",
        headline,
    )

    by_platform = query(BY_PLATFORM_SQL, **params)
    _print_section("Phase 1 visits by platform", by_platform)

    by_landing = query(BY_LANDING_SQL, **params)
    _print_section(
        "Phase 1 visits by platform + landing page (top 25)",
        by_landing,
    )

    link_perf = query(LINK_PERFORMANCE_SQL, **{
        "start": PHASE1_START,
        "end": PHASE1_END,
        "campaign_tag": PHASE1_CAMPAIGN_TAG,
    })
    _print_section(
        "Phase 1 per-link performance (utm.py-generated URLs)",
        link_perf,
    )

    etsy_yoy = query(ETSY_YOY_SQL)
    _print_section(
        "Etsy Social Media traffic, YoY (annual totals from Etsy PDFs)",
        etsy_yoy,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
