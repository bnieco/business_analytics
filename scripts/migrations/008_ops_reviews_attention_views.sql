-- =============================================================================
-- Migration 008: ops views for Etsy reviews + listing-attention (Task #64)
-- Date:     2026-06-04
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Surface the Task #63 raw data (raw.etsy_reviews API-era columns +
--           raw.etsy_listing_stats_daily) on the live phase1-dashboard via two
--           agent/dashboard-queryable ops views, following the existing
--           ops.* convention (CREATE OR REPLACE VIEW + COMMENT ON VIEW, owned
--           by brian_admin, GRANT SELECT to the read roles).
--
--   (1) ops.review_summary    — one row per listing that has reviews, with
--       count + avg rating + a per-star distribution, joined to the listing
--       title (raw.etsy_listings, API rows). A second materialization path is
--       NOT used; the dashboard derives overall totals + latest-N by querying
--       the companion ops.review_recent view (latest reviews, reviewer-safe).
--
--   (2) ops.listing_attention — one row per (listing_id, snapshot_date) with
--       views / num_favorers and day-over-day deltas computed with window
--       functions (LAG over snapshot_date per listing). Correct with a SINGLE
--       snapshot day: LAG returns NULL, so the deltas are NULL (not 0, not an
--       error). Joined to title + state. New listings appear automatically as
--       soon as the daily loader writes their first snapshot row.
--
-- Design notes:
--   * Views only — NO writes to raw.*. Fully reversible (DROP VIEW at bottom).
--   * Reviewer-safe: review_recent exposes only what Etsy shows publicly
--     (rating, message text, date, listing title). No buyer_user_id, no
--     order_id, no reviewer name column is surfaced.
--   * Title join uses raw.etsy_listings API rows (listing_id IS NOT NULL).
--     A review whose listing_id no longer maps to a current listing still
--     counts (title falls back to a '#<listing_id>' label) so totals stay honest.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Title lookup helper expression is inlined per-view (no extra view) to keep
-- the migration self-contained and the dependency graph flat.
-- -----------------------------------------------------------------------------

-- (1a) ops.review_summary — per-listing rollup (the dashboard's "per-listing
--      ratings" panel reads this; overall headline is a simple aggregate over it).
CREATE OR REPLACE VIEW ops.review_summary AS
WITH titled AS (
    SELECT
        r.review_id,
        r.listing_id,
        r.star_rating,
        r.message,
        r.reviewed_at,
        r.transaction_id,
        l.title AS listing_title
    FROM raw.etsy_reviews r
    LEFT JOIN raw.etsy_listings l
        ON l.listing_id = r.listing_id
       AND l.listing_id IS NOT NULL
)
SELECT
    COALESCE(listing_id::text, '(no listing)')                       AS listing_key,
    listing_id,
    COALESCE(MAX(listing_title), '#' || listing_id::text, '(legacy / no listing id)')
                                                                     AS listing_title,
    COUNT(*)                                                         AS review_count,
    ROUND(AVG(star_rating)::numeric, 2)                             AS avg_rating,
    COUNT(*) FILTER (WHERE star_rating = 5)                         AS stars_5,
    COUNT(*) FILTER (WHERE star_rating = 4)                         AS stars_4,
    COUNT(*) FILTER (WHERE star_rating = 3)                         AS stars_3,
    COUNT(*) FILTER (WHERE star_rating = 2)                         AS stars_2,
    COUNT(*) FILTER (WHERE star_rating = 1)                         AS stars_1,
    MIN(reviewed_at)                                                AS first_review,
    MAX(reviewed_at)                                                AS last_review
FROM titled
GROUP BY listing_id
ORDER BY review_count DESC, avg_rating DESC;

COMMENT ON VIEW ops.review_summary IS
    'Per-listing Etsy review rollup (Task #64): count, avg rating, per-star distribution, title, first/last review date. One row per listing_id (NULL listing_id = legacy CSV-era reviews grouped). Dashboard reviews panel + agent context source.';

-- (1b) ops.review_recent — latest reviews, reviewer-safe (no buyer PII beyond
--      what Etsy shows publicly). The dashboard latest-N ticker reads this.
CREATE OR REPLACE VIEW ops.review_recent AS
SELECT
    r.review_id,
    r.star_rating,
    -- public message text only; left as-is (Etsy shows it publicly). Trimmed
    -- for display in the renderer, not here, so the view stays lossless.
    r.message,
    r.reviewed_at,
    r.listing_id,
    COALESCE(l.title, '#' || r.listing_id::text, '(legacy review)')  AS listing_title,
    (r.transaction_id IS NOT NULL)                                  AS is_api_era
FROM raw.etsy_reviews r
LEFT JOIN raw.etsy_listings l
    ON l.listing_id = r.listing_id
   AND l.listing_id IS NOT NULL
ORDER BY r.reviewed_at DESC NULLS LAST, r.review_id DESC;

COMMENT ON VIEW ops.review_recent IS
    'Latest Etsy reviews, reviewer-safe (Task #64): rating, public message, date, listing title, is_api_era flag. No buyer_user_id/order_id/name surfaced. Ordered newest-first. Dashboard latest-reviews ticker + agent context.';

-- -----------------------------------------------------------------------------
-- (2) ops.listing_attention — per-listing per-day views/favorers + DoD deltas.
--     Window functions LAG over snapshot_date per listing. With ONE snapshot
--     day, LAG -> NULL -> deltas NULL (honest "no trend yet"), never an error.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ops.listing_attention AS
WITH snaps AS (
    SELECT
        s.listing_id,
        s.snapshot_date,
        s.state,
        s.views,
        s.num_favorers,
        s.quantity,
        LAG(s.views)        OVER w AS prev_views,
        LAG(s.num_favorers) OVER w AS prev_favorers,
        LAG(s.snapshot_date) OVER w AS prev_snapshot_date
    FROM raw.etsy_listing_stats_daily s
    WINDOW w AS (PARTITION BY s.listing_id ORDER BY s.snapshot_date)
)
SELECT
    snaps.listing_id,
    snaps.snapshot_date,
    COALESCE(l.title, '#' || snaps.listing_id::text)                AS listing_title,
    snaps.state,
    snaps.views,
    snaps.num_favorers,
    snaps.quantity,
    -- Deltas: NULL when there is no prior snapshot (single-day case).
    (snaps.views        - snaps.prev_views)                        AS views_delta,
    (snaps.num_favorers - snaps.prev_favorers)                     AS favorers_delta,
    snaps.prev_snapshot_date,
    (snaps.prev_snapshot_date IS NOT NULL)                         AS has_prior
FROM snaps
LEFT JOIN raw.etsy_listings l
    ON l.listing_id = snaps.listing_id
   AND l.listing_id IS NOT NULL
ORDER BY snaps.snapshot_date DESC, snaps.views DESC NULLS LAST;

COMMENT ON VIEW ops.listing_attention IS
    'Per-listing per-day Etsy attention (Task #64): views, num_favorers, quantity + day-over-day deltas (LAG over snapshot_date per listing) joined to title + state. Deltas NULL when no prior snapshot (single-day = honest "trend builds daily"). New listings appear automatically on first snapshot. Dashboard attention panel + agent context.';

-- -----------------------------------------------------------------------------
-- GRANTs — follow the ops.* convention: dashboard role (brian_admin already
-- owns these as creator/superuser) plus the read roles so agents/MCP can query.
-- forge_ops + mcp_readonly cannot see raw.* directly; SELECT on these ops views
-- gives them the safe, derived projection.
-- -----------------------------------------------------------------------------
GRANT SELECT ON ops.review_summary    TO forge_ops, mcp_readonly;
GRANT SELECT ON ops.review_recent     TO forge_ops, mcp_readonly;
GRANT SELECT ON ops.listing_attention TO forge_ops, mcp_readonly;

COMMIT;

-- =============================================================================
-- ROLLBACK (manual; run only to fully reverse this migration)
-- -----------------------------------------------------------------------------
-- BEGIN;
-- DROP VIEW IF EXISTS ops.listing_attention;
-- DROP VIEW IF EXISTS ops.review_recent;
-- DROP VIEW IF EXISTS ops.review_summary;
-- COMMIT;
-- =============================================================================
