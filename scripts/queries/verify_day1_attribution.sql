-- =============================================================================
-- verify_day1_attribution.sql — diagnostic, NOT production attribution
-- Owner:  Edison (Analytics Engineer)
-- Created: 2026-05-24 (round-2 UTM investigation; pre-staged for 5/26–5/27)
--
-- PURPOSE
--   On 2026-05-24 Brian posted his Day 1 Phase 1 reactivation links. His
--   in-browser test of the FB → Fear Tracker (listing 4364365992) link
--   showed the FULL UTM tuple PRESERVED on Etsy's landing URL:
--
--     https://www.etsy.com/listing/4364365992/daggerheart-fear-tracker-abacus-style
--       ?etsrc=sdt
--       &utm_source=facebook
--       &utm_medium=organic
--       &utm_campaign=phase1_reactivation
--       &utm_content=fearTracker_hero_day1
--       &fbclid=IwY2xjawSAZ...
--
--   This contradicts Edison's round-1 conclusion (encoded in
--   phase1_link_performance.sql) that Etsy strips utm_content. We need to
--   know what GA4 actually CAPTURED for real Day 1 visits, not just what
--   the address bar showed in Brian's manual test.
--
--   GA4 → BigQuery export is T+24–72hr. Run this once raw.ga4_events has
--   event_date = '2026-05-24' rows present (expected 5/26 or 5/27).
--
--   This query is DIAGNOSTIC ONLY. Do not wire it into production
--   attribution until the broader round-2 test matrix resolves the
--   destination-type vs referrer-specific question.
--
-- WHAT TO LOOK FOR
--   For each row returned:
--     1. collected_traffic_source_source   — should equal 'facebook' if UTM survived
--     2. collected_traffic_source_campaign — should equal 'phase1_reactivation'
--     3. ep_content (extracted from event_params) — should equal
--        'fearTracker_hero_day1' if utm_content survived
--     4. page_location — confirms the full landing URL GA4 recorded,
--        including whether utm_* params are in the query string
--     5. traffic_source_source / _medium — GA4's session-level attribution
--        (may differ from collected_* if GA4 reclassified)
--
--   DECISION RULES:
--     - If (3) ep_content = 'fearTracker_hero_day1' for ANY visit:
--         → utm_content IS recoverable for FB → listing landings.
--         → Round-1 generalization was wrong for this case.
--         → Per-post attribution to listings via utm_content is viable;
--           revisit Brief Enrichment Protocol + phase1_link_performance.sql.
--
--     - If (3) is NULL on every visit but page_location SHOWS utm_content
--       in the query string:
--         → UTM survived the redirect but GA4 didn't ingest it.
--         → Likely Etsy's GA4 property config, not URL stripping.
--         → Different problem; document and stop.
--
--     - If (3) is NULL AND page_location has no utm_content:
--         → For Day 1 FB visits specifically, UTM did not reach GA4.
--         → Brian's in-browser snapshot may have been atypical (e.g. his
--           own session, cached state). Still need the round-2 test matrix.
--
-- Run with:
--   ./venv/bin/python scripts/run_query.py scripts/queries/verify_day1_attribution.sql
-- or
--   psql "$BUSINESS_DB_URL" -f scripts/queries/verify_day1_attribution.sql
-- =============================================================================

\set target_date  '2026-05-24'
\set target_listing '4364365992'

\echo
\echo === Row count: Day 1 Fear Tracker (4364365992) page_view events ===
SELECT
    COUNT(*)                                       AS total_events,
    COUNT(*) FILTER (WHERE traffic_source_medium = 'referral')                       AS referral_events,
    COUNT(*) FILTER (WHERE traffic_source_source ~* 'facebook')                      AS facebook_referrer_events,
    COUNT(*) FILTER (WHERE collected_traffic_source_campaign = 'phase1_reactivation') AS phase1_campaign_events
FROM raw.ga4_events
WHERE event_date = DATE :'target_date'
  AND event_name = 'page_view'
  AND page_location ~ ('/listing/' || :'target_listing');

\echo
\echo === Detail: what GA4 actually captured per visit ===
\echo Look at ep_content -- if any row shows 'fearTracker_hero_day1', utm_content survived to GA4.
WITH listing_visits AS (
    SELECT
        e.event_timestamp,
        e.user_pseudo_id,
        e.ga_session_id,
        e.traffic_source_source,
        e.traffic_source_medium,
        e.collected_traffic_source_source,
        e.collected_traffic_source_medium,
        e.collected_traffic_source_campaign,
        e.page_location,
        e.page_referrer,
        e.event_params
    FROM raw.ga4_events e
    WHERE e.event_date = DATE :'target_date'
      AND e.event_name = 'page_view'
      AND e.page_location ~ ('/listing/' || :'target_listing')
)
SELECT
    to_timestamp(event_timestamp / 1000000)::timestamptz AT TIME ZONE 'America/Los_Angeles' AS visit_pt,
    LEFT(user_pseudo_id, 12)                AS pseudo_id_prefix,
    ga_session_id,
    traffic_source_source                   AS ga_source,
    traffic_source_medium                   AS ga_medium,
    collected_traffic_source_source         AS coll_source,
    collected_traffic_source_medium         AS coll_medium,
    collected_traffic_source_campaign       AS coll_campaign,
    -- Pull the four UTM-bearing event_params explicitly. GA4 sometimes
    -- stores these as string_value, sometimes (older exports) as the raw
    -- 'value' shape; check both to be safe.
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'source'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'source')
    )                                       AS ep_source,
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'medium'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'medium')
    )                                       AS ep_medium,
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'campaign'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'campaign')
    )                                       AS ep_campaign,
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content')
    )                                       AS ep_content,
    page_referrer,
    page_location
FROM listing_visits
ORDER BY event_timestamp;

\echo
\echo === Roll-up: did ANY Day 1 visit carry utm_content = 'fearTracker_hero_day1'? ===
SELECT
    COUNT(*)                                             AS visits_with_utm_content,
    COUNT(DISTINCT user_pseudo_id)                       AS unique_visitors_with_utm_content
FROM raw.ga4_events e
WHERE e.event_date = DATE :'target_date'
  AND e.event_name = 'page_view'
  AND e.page_location ~ ('/listing/' || :'target_listing')
  AND COALESCE(
        (SELECT ep -> 'value' ->> 'string_value' FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content'),
        (SELECT ep ->> 'value'                   FROM jsonb_array_elements(event_params) ep WHERE ep ->> 'key' = 'content')
      ) = 'fearTracker_hero_day1';

\echo
\echo === Cross-check: query-string presence of utm_content in page_location ===
\echo This tells us whether the LANDING URL carried utm_content even if GA4
\echo did not extract it into event_params.
SELECT
    COUNT(*)                                                              AS visits_with_qs_utm_content,
    COUNT(*) FILTER (WHERE page_location ~ 'utm_content=fearTracker_hero_day1') AS visits_with_exact_qs_match
FROM raw.ga4_events
WHERE event_date = DATE :'target_date'
  AND event_name = 'page_view'
  AND page_location ~ ('/listing/' || :'target_listing')
  AND page_location ~ 'utm_content=';
