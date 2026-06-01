-- =============================================================================
-- phase1_link_performance.sql — per-UTM-link visit performance for Phase 1
-- Owner:  Edison (Analytics Engineer)
-- Window: 2026-05-24 → 2026-08-22
--
-- ATTRIBUTION NOTE (rewritten 2026-05-27, round 4, Edison):
--   Round-3 (5/24) Etsy attribution joined on
--     `traffic_source_medium = 'referral' AND traffic_source_source ~
--      <hostname regex per utm_source>`
--   The 5/24 design assumption was that social → Etsy clicks would land in
--   GA4 with referer-based attribution (hostname source values like
--   'facebook.com' + medium = 'referral').
--
--   Production data verified 2026-05-27 contradicted that assumption.
--   UTM-tagged Phase 1 traffic lands in GA4 with LITERAL utm_* values:
--     traffic_source_source = 'facebook'   (NOT 'facebook.com')
--     traffic_source_medium = 'organic'    (NOT 'referral')
--     collected_traffic_source_source = 'facebook'
--     collected_traffic_source_campaign = 'phase1_reactivation'
--   GA4 takes utm_source / utm_medium verbatim from the URL query string.
--   The round-3 JOIN matched zero events for every Phase 1 link (verified
--   2026-05-27: 7 links generated, all returned 0 attributed events under
--   the round-3 filter, including the Day 1 FB Fear Tracker click that
--   verify_day1_attribution.py confirmed is in raw.ga4_events).
--
--   Round-4 fix: the Etsy branch now joins on the literal collected_traffic_
--   source_source/campaign columns (matching what GA4 actually wrote), and
--   uses event_params for utm_content discrimination since raw.ga4_events
--   has no collected_traffic_source_content column. The own-site branch
--   was already correct (already used collected_traffic_source_*) and is
--   unchanged.
--
--   With the round-4 logic both Etsy and own-site destinations attribute on
--   the full UTM tuple (source + campaign + content), so the
--   `attribution_mode` column now collapses to a single value (`per_post`).
--   The round-3 `per_post_with_known_gap` / `unattributable` distinctions
--   were artifacts of the referrer-based design and no longer apply: there
--   is no platform-regex mapping anymore (we match utm_source literally),
--   and there is no Pinterest-bio outlier in the join logic (utm_content
--   from event_params is the per-post discriminator regardless of which
--   platform sent the click). If a Pinterest-bio path strips utm_content,
--   that visit will fail this join AND will not appear in the campaign-tag
--   rollup in phase1_progress.sql either — it'd surface only if some other
--   utm_* field made it through. That's an honest data-loss case for the
--   bio-link wrapper, not a per-platform-attribution mode.
--
--   Operational lesson (encoded here so future-Edison reads it before
--   touching production attribution again): round-3 was encoded into
--   production SQL on the strength of the 6-cell browser test matrix
--   without verifying against actual GA4-ingested production data. The
--   matrix confirmed UTMs survived URL transport end-to-end; it did NOT
--   confirm what GA4 wrote into traffic_source_* vs
--   collected_traffic_source_* after ingestion. Verify against the
--   downstream artifact you actually filter on, not the upstream artifact
--   that feeds it. See Edison persona's "Verify before encoding" principle
--   and feedback_verify_before_act.md.
--
--   Basis for round-4: 2026-05-27 query of raw.ga4_events covering
--   2026-05-24 → 2026-05-25 (loaded by 5/27 06:00 cron). Day 1 FB →
--   listing 4364365992 click present with collected_traffic_source_source =
--   'facebook', collected_traffic_source_campaign = 'phase1_reactivation',
--   event_params content = 'fearTracker_hero_day1' — matches link id=6
--   under the round-4 join (verified via direct SELECT before encoding).
--
-- Companion to scripts/queries/phase1_progress.sql — that file shows
-- aggregate campaign-rollup counts; this one walks every link Brian
-- generated via scripts/utm.py and reports its attributed visit count.
--
-- Run with:
--   ./venv/bin/python scripts/run_query.py scripts/queries/phase1_link_performance.sql
-- or
--   psql "$BUSINESS_DB_URL" -f scripts/queries/phase1_link_performance.sql
-- =============================================================================

\set phase1_start '2026-05-24'
\set phase1_end   '2026-08-22'

\echo
\echo === Phase 1 per-link performance (all links generated via utm.py) ===
\echo NOTE: Both Etsy and own-site destinations attribute per-post on the
\echo full UTM tuple (utm_source + utm_campaign + utm_content). Source and
\echo campaign come from GA4's collected_traffic_source_* columns;
\echo utm_content comes from event_params (no collected_traffic_source_content
\echo column exists in raw.ga4_events). Round-4 (2026-05-27) — see file header.

WITH
links AS (
    SELECT
        l.*,
        CASE
            WHEN l.base_url ~* '(^|//|\.)etsy\.com(/|\?|$)' THEN 'etsy'
            ELSE 'own_site'
        END AS destination_kind
    FROM raw.utm_links l
    WHERE l.utm_campaign = 'phase1_reactivation'
),
-- Branch A: Etsy-destined links. Join on the literal utm values GA4
-- ingested into collected_traffic_source_source/campaign, plus utm_content
-- from event_params. Listing-id guard preserves the original sanity check
-- (a link pointing at /listing/N should only attribute visits that landed
-- on /listing/N).
etsy_attribution AS (
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
     AND e.event_date BETWEEN DATE :'phase1_start' AND DATE :'phase1_end'
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND (
         l.base_url !~ '/listing/[0-9]+'
         OR e.page_location ~ ('/listing/' || substring(l.base_url FROM '/listing/([0-9]+)'))
     )
     AND COALESCE(
             (SELECT ep -> 'value' ->> 'string_value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1),
             (SELECT ep ->> 'value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1)
         ) IS NOT DISTINCT FROM l.utm_content
    GROUP BY l.id
),
-- Branch B: own-site links. Same shape as Branch A; only differs in the
-- destination_kind guard. utm_content still comes from event_params.
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
     AND e.event_date BETWEEN DATE :'phase1_start' AND DATE :'phase1_end'
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND COALESCE(
             (SELECT ep ->> 'value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1),
             (SELECT ep -> 'value' ->> 'string_value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1)
         ) IS NOT DISTINCT FROM l.utm_content
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
    -- Both branches now use the full UTM tuple, so attribution_mode is
    -- uniform. The round-3 per_post_with_known_gap / unattributable
    -- categories were artifacts of the referrer-based design; see the
    -- ATTRIBUTION NOTE in the file header.
    'per_post'                      AS attribution_mode,
    l.created_by,
    l.notes
FROM links l
LEFT JOIN attribution a ON a.link_id = l.id
ORDER BY visits DESC, l.created_at DESC;


\echo
\echo === Phase 1 link-performance summary ===
WITH
links AS (
    SELECT
        l.id,
        l.utm_source,
        l.utm_content,
        l.utm_campaign,
        l.base_url,
        CASE
            WHEN l.base_url ~* '(^|//|\.)etsy\.com(/|\?|$)' THEN 'etsy'
            ELSE 'own_site'
        END AS destination_kind
    FROM raw.utm_links l
    WHERE l.utm_campaign = 'phase1_reactivation'
),
etsy_attribution AS (
    -- Mirrors the per-post Etsy join in the main SELECT above. See the
    -- ATTRIBUTION NOTE in the file header for the round-4 rationale.
    SELECT l.id AS link_id, COUNT(*) AS visits
    FROM links l
    JOIN raw.ga4_events e
      ON l.destination_kind = 'etsy'
     AND e.event_name = 'page_view'
     AND e.event_date BETWEEN DATE :'phase1_start' AND DATE :'phase1_end'
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND (
         l.base_url !~ '/listing/[0-9]+'
         OR e.page_location ~ ('/listing/' || substring(l.base_url FROM '/listing/([0-9]+)'))
     )
     AND COALESCE(
             (SELECT ep -> 'value' ->> 'string_value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1),
             (SELECT ep ->> 'value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1)
         ) IS NOT DISTINCT FROM l.utm_content
    GROUP BY l.id
),
own_site_attribution AS (
    SELECT l.id AS link_id, COUNT(*) AS visits
    FROM links l
    JOIN raw.ga4_events e
      ON l.destination_kind = 'own_site'
     AND e.event_name = 'page_view'
     AND e.event_date BETWEEN DATE :'phase1_start' AND DATE :'phase1_end'
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND COALESCE(
             (SELECT ep ->> 'value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1),
             (SELECT ep -> 'value' ->> 'string_value'
              FROM jsonb_array_elements(e.event_params) ep
              WHERE ep ->> 'key' = 'content'
              LIMIT 1)
         ) IS NOT DISTINCT FROM l.utm_content
    GROUP BY l.id
),
attribution AS (
    SELECT * FROM etsy_attribution
    UNION ALL
    SELECT * FROM own_site_attribution
)
SELECT
    COUNT(*)                                              AS links_generated,
    COUNT(*) FILTER (WHERE l.destination_kind = 'etsy')   AS etsy_links,
    COUNT(*) FILTER (WHERE l.destination_kind = 'own_site') AS own_site_links,
    COUNT(*) FILTER (WHERE COALESCE(a.visits, 0) > 0)     AS links_with_visits,
    COUNT(*) FILTER (WHERE COALESCE(a.visits, 0) = 0)     AS links_with_zero_visits,
    COALESCE(SUM(a.visits), 0)                            AS total_attributed_visits
FROM links l
LEFT JOIN attribution a ON a.link_id = l.id;
