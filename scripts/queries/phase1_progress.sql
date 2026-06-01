-- =============================================================================
-- phase1_progress.sql — Multi-Channel Reactivation Pilot, Phase 1 weekly check
-- Owner:  Edison (Analytics Engineer)
-- Window: 2026-05-24 → 2026-08-22 (90-day organic pilot; Day 0 prep 2026-05-23)
--
-- Focused on Phase 1 → Phase 2 decision criterion 4:
--   "Organic-attributed Etsy + eldersninerelics.com referrals cross ~500 visits
--    over 90 days (~4× YTD baseline of 121)."
--
-- ATTRIBUTION NOTE (rewritten 2026-05-27, round 4, Edison):
--   Round-3 (5/24) attribution was REFERRER-FLOOR: filter
--   `traffic_source_medium = 'referral' AND traffic_source_source ~
--   <hostname regex>` on raw.ga4_events. The 5/24 design assumption was that
--   social→Etsy clicks would land in GA4 with referer-based attribution
--   (hostname source values like 'facebook.com' + medium = 'referral').
--
--   That assumption was wrong. Production data verified 2026-05-27 shows
--   UTM-tagged Phase 1 traffic produces LITERAL utm_* values in GA4:
--     traffic_source_source = 'facebook'   (NOT 'facebook.com')
--     traffic_source_medium = 'organic'    (NOT 'referral')
--   GA4 takes the utm_source / utm_medium query-string values verbatim. The
--   round-3 filter therefore matched ZERO Phase 1 events. The first formal
--   weekly review (Day 7 = Sat 2026-05-30) would have reported zero, masking
--   the real Day 1 FB Fear Tracker click + Day 3 Pinterest hero tests.
--
--   Round-4 fix: anchor the Phase 1 filter on the canonical CAMPAIGN TAG
--   that scripts/utm.py stamps on every Phase 1 link:
--     collected_traffic_source_campaign = 'phase1_reactivation'
--   The canonical campaign tag is the load-bearing identifier — utm.py
--   defaults it for every generated link, and Brian's Phase 1 ops discipline
--   is built around it. Filtering on campaign instead of platform-referrer
--   is more robust:
--     - Catches every Phase 1 UTM-tagged event regardless of which platform
--       the click came from, including paths where GA4 attributes source to
--       '(direct)' because the click came through a redirect/wrapper.
--     - Naturally EXCLUDES phase1_diag test-matrix events (2026-05-24,
--       Brian's 6-cell browser test) — those carry `phase1_diag`, a
--       distinct tag. Test traffic stays out of production counts.
--     - Naturally EXCLUDES the Day 2 (2026-05-25) inthalo Meta-Ads paid
--       click — that carries `inthalo`, not phase1_reactivation, so the
--       organic-only dashboard correctly leaves it out.
--     - Naturally EXCLUDES (direct)/(none) organic non-pilot traffic.
--
--   What about per-post (utm_content) attribution?
--   That lives in phase1_link_performance.sql, which now joins
--   raw.utm_links to raw.ga4_events on the literal collected_traffic_source_*
--   columns (matching what's actually in production). This file stays at the
--   campaign-level rollup for criterion-4 progress.
--
--   What about Etsy "Social Media" YoY context?
--   Section 4 below still reads raw.etsy_traffic_stats unchanged — that's
--   Etsy's own annual aggregate, independent of GA4 attribution mechanics.
--
--   Operational lesson (kept in the production file so future-Edison reads
--   it before touching attribution again): the round-3 referrer-floor design
--   was encoded into production SQL on the strength of the 6-cell browser
--   test matrix WITHOUT a verification pass against actual GA4-ingested
--   production data. The matrix confirmed UTMs survived URL transport
--   end-to-end; it did NOT confirm what GA4 wrote into traffic_source_*
--   vs collected_traffic_source_* after ingestion. Verify against the
--   downstream artifact you actually filter on, not the upstream artifact
--   that feeds it. (See Edison persona's "Verify before encoding" principle
--   and feedback_verify_before_act.md.)
--
--   Basis for round-4: 2026-05-27 query of raw.ga4_events covering
--   2026-05-24 → 2026-05-25 (loaded by 5/27 06:00 cron). 4 distinct
--   campaign tag values observed: NULL (direct/none traffic),
--   phase1_diag (test), phase1_reactivation (Day 1 FB click — the one
--   event this dashboard now correctly surfaces), inthalo (paid Meta
--   Ads). Filter logic confirmed against this evidence before encoding.
--
-- Run with:
--   ./venv/bin/python scripts/run_query.py scripts/queries/phase1_progress.sql
-- or
--   psql "$BUSINESS_DB_URL" -f scripts/queries/phase1_progress.sql
--
-- Outputs four result sets:
--   1. Headline: visits-to-date, days remaining, % of 500-visit floor.
--   2. By platform (collected_traffic_source_source) — which channels are driving.
--   3. By landing page — same campaign-filter, grouped by platform + URL.
--   4. Etsy "Social Media" YoY context from raw.etsy_traffic_stats.
-- =============================================================================

-- Window constants live here so changes to the pilot dates only happen once.
-- 90 days inclusive of the start date.
\set phase1_start '2026-05-24'
\set phase1_end   '2026-08-22'
\set phase1_target_visits 500

-- Canonical Phase 1 campaign tag. scripts/utm.py stamps this on every
-- generated link via DEFAULT_CAMPAIGN; the Brief Enrichment Protocol locks
-- the convention. Keep in sync with scripts/phase1_progress.py and
-- scripts/utm.py.
\set phase1_campaign_tag 'phase1_reactivation'

-- -----------------------------------------------------------------------------
-- 1. Headline scoreboard: criterion-4 progress at a glance.
-- -----------------------------------------------------------------------------
\echo
\echo === Phase 1 headline (criterion 4: 500-visit organic floor) ===
WITH visits AS (
    SELECT COUNT(*) AS phase1_visits
    FROM raw.ga4_events
    WHERE event_date BETWEEN DATE :'phase1_start' AND DATE :'phase1_end'
      AND event_name = 'page_view'
      AND collected_traffic_source_campaign = :'phase1_campaign_tag'
)
SELECT
    DATE :'phase1_start'                                         AS phase1_start,
    DATE :'phase1_end'                                           AS phase1_end,
    CURRENT_DATE                                                 AS as_of,
    GREATEST(0, (DATE :'phase1_end' - CURRENT_DATE))             AS days_remaining,
    LEAST(CURRENT_DATE, DATE :'phase1_end')
        - DATE :'phase1_start' + 1                                AS days_elapsed,
    v.phase1_visits                                              AS visits_to_date,
    :phase1_target_visits                                        AS visits_floor,
    ROUND(100.0 * v.phase1_visits / :phase1_target_visits, 1)    AS pct_of_floor,
    -- Trajectory: at current pace, what does the 90-day total look like?
    CASE
        WHEN (LEAST(CURRENT_DATE, DATE :'phase1_end')
              - DATE :'phase1_start' + 1) > 0
        THEN ROUND(
            v.phase1_visits::numeric
            * 90.0
            / (LEAST(CURRENT_DATE, DATE :'phase1_end')
               - DATE :'phase1_start' + 1)
        )
        ELSE NULL
    END                                                          AS pace_projection_90d
FROM visits v;


-- -----------------------------------------------------------------------------
-- 2. Visits by platform. Uses collected_traffic_source_source, which carries
--    the literal utm_source value GA4 received (e.g. 'facebook', 'pinterest')
--    rather than a hostname-style referrer. For Phase 1 UTM-tagged traffic
--    this is the reliable per-platform discriminator.
-- -----------------------------------------------------------------------------
\echo
\echo === Phase 1 visits by platform ===
SELECT
    COALESCE(collected_traffic_source_source, '(unset)')         AS platform,
    COUNT(*)                                                     AS visits,
    COUNT(DISTINCT user_pseudo_id)                               AS unique_visitors,
    COUNT(DISTINCT ga_session_id)                                AS sessions,
    MIN(event_date)                                              AS first_seen,
    MAX(event_date)                                              AS last_seen
FROM raw.ga4_events
WHERE event_date BETWEEN DATE :'phase1_start' AND DATE :'phase1_end'
  AND event_name = 'page_view'
  AND collected_traffic_source_campaign = :'phase1_campaign_tag'
GROUP BY 1
ORDER BY visits DESC;


-- -----------------------------------------------------------------------------
-- 3. Per-landing-page view. Groups Phase 1 visits by (platform, page_location)
--    so we can see which posts drove visits to which Etsy listings / own-site
--    pages. Per-post (utm_content) discrimination lives in
--    phase1_link_performance.sql.
-- -----------------------------------------------------------------------------
\echo
\echo === Phase 1 visits by platform + landing page (top 25) ===
SELECT
    COALESCE(collected_traffic_source_source, '(unset)')         AS platform,
    -- Strip query string so the same listing with different ?variation0=
    -- collapses into one row.
    regexp_replace(page_location, '\?.*$', '')                   AS landing_page,
    COUNT(*)                                                     AS visits,
    COUNT(DISTINCT user_pseudo_id)                               AS unique_visitors,
    MIN(event_date)                                              AS first_seen,
    MAX(event_date)                                              AS last_seen
FROM raw.ga4_events
WHERE event_date BETWEEN DATE :'phase1_start' AND DATE :'phase1_end'
  AND event_name = 'page_view'
  AND collected_traffic_source_campaign = :'phase1_campaign_tag'
GROUP BY 1, 2
ORDER BY visits DESC
LIMIT 25;


-- -----------------------------------------------------------------------------
-- 4. Etsy "Social Media" YoY context from raw.etsy_traffic_stats.
--    Etsy stats are annual totals per (year, category, sub_source), so this is
--    coarse: it tells us whether 2026's social-media visit count is meaningfully
--    above 2025's, but it can't be sliced into a current-week comparison.
--    Treat it as supporting context for criterion 4, not a primary measure.
-- -----------------------------------------------------------------------------
\echo
\echo === Etsy Social Media traffic, YoY (annual totals from Etsy PDFs) ===
SELECT
    year,
    sub_source,
    visits,
    date_range_start,
    date_range_end
FROM raw.etsy_traffic_stats
WHERE category = 'Social Media'
ORDER BY year DESC, visits DESC NULLS LAST, sub_source NULLS FIRST;
