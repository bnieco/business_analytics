-- 003_raw_etsy_traffic_stats.sql
-- Creates raw.etsy_traffic_stats for Etsy Shop Manager Stats "Referrer detail" PDFs.
--
-- Background: Etsy's Shop Manager Stats > Traffic details page shows the 6
-- top-level traffic categories (Etsy Search, Etsy App & other Etsy pages,
-- Etsy Marketing & SEO, Advertising, Social Media, Direct & Other Traffic)
-- and, for 4 of them, a "Traffic source breakdown" table listing the
-- specific referring domains (e.g. m.facebook.com, www.google.com). The
-- only export format Etsy offers is PDF.
--
-- 4 categories expose sub-source breakdowns:
--   - Social Media         (m.facebook.com, instagram.com, ...)
--   - Direct & Other       (zee.am, zee.store, www.tiktok.com, ...)
--   - Etsy Marketing & SEO (www.google.com, www.bing.com, ...)
--   - Advertising          (www.etsy.com)
--
-- 2 categories do NOT expose sub-source breakdowns because they ARE
-- singular sources (Etsy Search, Etsy App & other Etsy pages). For these,
-- one row per (year, category) is loaded with sub_source = NULL and a
-- visit count taken from the category-summary screenshot total.
--
-- One row per (year, category, sub_source). sub_source NULL means
-- "category total only, no breakdown available".
--
-- Idempotency: unique constraint on (year, category, sub_source) plus
-- ON CONFLICT DO UPDATE in the loader so re-runs refresh visit counts
-- without duplicating rows.

CREATE TABLE IF NOT EXISTS raw.etsy_traffic_stats (
    id                  BIGSERIAL PRIMARY KEY,

    -- Identifiers
    year                SMALLINT NOT NULL,
    category            TEXT     NOT NULL,   -- Normalized: 'Etsy Search', 'Etsy App',
                                              -- 'Etsy Marketing SEO', 'Advertising',
                                              -- 'Social Media', 'Direct & Other'
    sub_source          TEXT,                 -- Referring domain, e.g. 'm.facebook.com'.
                                              -- NULL = category total only (Etsy Search,
                                              -- Etsy App) or a "total" reconciliation row.

    visits              INTEGER  NOT NULL,

    -- Date range covered by the source PDF (from "Date Range" header)
    date_range_start    DATE,
    date_range_end      DATE,

    -- Provenance
    source_pdf          TEXT,                 -- Filename of the source PDF
    notes               TEXT,                 -- Free-text flag, e.g. "category total
                                              -- from screenshot — PDF page-1 only
                                              -- captured top-N sub-sources"
    loaded_at           TIMESTAMPTZ DEFAULT NOW()
);

-- NULL sub_source must still be unique per (year, category) — Postgres
-- treats NULLs as distinct in unique indexes by default, so use NULLS NOT
-- DISTINCT (PG 15+) to enforce idempotent upsert of category-total rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_etsy_traffic_stats_yr_cat_sub
    ON raw.etsy_traffic_stats (year, category, sub_source)
    NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS idx_etsy_traffic_stats_year_category
    ON raw.etsy_traffic_stats (year, category);

COMMENT ON TABLE raw.etsy_traffic_stats IS
    'Etsy Shop Manager Stats referrer detail. One row per (year, category, sub_source). sub_source NULL = category total (Etsy Search / Etsy App have no sub-breakdown, or backfilled from dashboard screenshot when PDF page-1 truncates sub-source list).';

COMMENT ON COLUMN raw.etsy_traffic_stats.category IS
    'Normalized top-level traffic category: Etsy Search, Etsy App, Etsy Marketing SEO, Advertising, Social Media, Direct & Other. Note: source dashboard labels "Advertising" as "Etsy Ads" in the summary view — same data.';

COMMENT ON COLUMN raw.etsy_traffic_stats.sub_source IS
    'Referring domain from Etsy "Traffic source breakdown". NULL when (a) category is Etsy Search or Etsy App (no breakdown available), or (b) row represents a backfilled category total.';
