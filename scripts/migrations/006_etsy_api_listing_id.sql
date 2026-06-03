-- =============================================================================
-- Migration 006: Etsy Open API v3 readiness
-- Date:     2026-06-03
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Prepare the CSV-era raw.etsy_* landing tables for the direct Etsy
--           Open API v3 loader (scripts/etsy_api.py).
--
--   1. raw.etsy_listings gained NO listing_id from the CSV export. The API's
--      getListingsByShop returns a stable numeric listing_id; add it (+ index)
--      so listings can be ID-keyed for delete-reload idempotency and joined to
--      raw.etsy_order_items.listing_id / raw.ga4_events.listing_id.
--
--   2. A small set of NULLABLE API-era columns the CSV lacked but the API
--      returns and we want (listing state, timestamps, currency on the API
--      money fields). All additive — CSV-era columns are untouched so
--      scripts/load_etsy.py (CSV fallback) stays loadable.
--
--   3. raw.etsy_api_load_state — mirrors raw.ga4_load_state. One row per logical
--      load "window" (entity + date-window key) tracking the high-water mark of
--      created timestamps, row count, and load bookkeeping.
--
-- Design notes:
--   * ADDITIVE + REVERSIBLE. No column drops, no type changes to existing
--     columns, no NOT NULL added to existing data. Rollback block at the bottom
--     (commented) drops only what this migration created.
--   * IDEMPOTENT: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS /
--     CREATE TABLE IF NOT EXISTS — safe to re-run.
--   * The loader keys idempotency on:
--       raw.etsy_orders.order_id        (receipt_id, varchar — already present + indexed)
--       raw.etsy_order_items.transaction_id (varchar — already present + indexed)
--       raw.etsy_listings.listing_id    (BIGINT — added here + indexed)
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. raw.etsy_listings.listing_id  (the gap the design doc calls out)
-- -----------------------------------------------------------------------------
ALTER TABLE raw.etsy_listings
    ADD COLUMN IF NOT EXISTS listing_id BIGINT;

CREATE INDEX IF NOT EXISTS etsy_listings_listing_id_idx
    ON raw.etsy_listings (listing_id)
    WHERE listing_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 2. NULLABLE API-era columns the CSV export lacked.
--    Kept nullable so CSV loads (which never populate them) remain valid.
-- -----------------------------------------------------------------------------

-- raw.etsy_listings: the API exposes listing lifecycle + timestamps the CSV omitted.
ALTER TABLE raw.etsy_listings
    ADD COLUMN IF NOT EXISTS state               VARCHAR,      -- active | inactive | expired | draft | sold_out
    ADD COLUMN IF NOT EXISTS created_timestamp   BIGINT,       -- epoch seconds (Etsy native)
    ADD COLUMN IF NOT EXISTS updated_timestamp   BIGINT,       -- epoch seconds; drives listings repull/diff
    ADD COLUMN IF NOT EXISTS url                 TEXT,
    ADD COLUMN IF NOT EXISTS _api_loaded_at      TIMESTAMPTZ;  -- set by etsy_api.py; NULL on CSV rows

-- raw.etsy_orders: receipt-level fields useful from the API (status changes,
-- timestamps) that the CSV either omitted or named differently. order_id already
-- carries the receipt_id; these are additive enrichments.
ALTER TABLE raw.etsy_orders
    ADD COLUMN IF NOT EXISTS receipt_id          BIGINT,       -- numeric mirror of order_id for clean ID keys
    ADD COLUMN IF NOT EXISTS created_timestamp   BIGINT,       -- epoch seconds; window high-water mark
    ADD COLUMN IF NOT EXISTS updated_timestamp   BIGINT,       -- epoch seconds; catches status changes
    ADD COLUMN IF NOT EXISTS is_paid             BOOLEAN,
    ADD COLUMN IF NOT EXISTS is_shipped          BOOLEAN,
    ADD COLUMN IF NOT EXISTS _api_loaded_at      TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS etsy_orders_receipt_id_idx
    ON raw.etsy_orders (receipt_id)
    WHERE receipt_id IS NOT NULL;

-- raw.etsy_order_items: the API returns a numeric listing_id per transaction and
-- a created timestamp. The CSV listing_id is varchar; add a numeric companion so
-- the loader can carry the API's BIGINT without disturbing the CSV column.
ALTER TABLE raw.etsy_order_items
    ADD COLUMN IF NOT EXISTS listing_id_num      BIGINT,       -- numeric listing_id from the API
    ADD COLUMN IF NOT EXISTS created_timestamp   BIGINT,       -- epoch seconds
    ADD COLUMN IF NOT EXISTS receipt_id          BIGINT,       -- parent receipt (numeric)
    ADD COLUMN IF NOT EXISTS _api_loaded_at      TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS etsy_order_items_listing_id_num_idx
    ON raw.etsy_order_items (listing_id_num)
    WHERE listing_id_num IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 3. raw.etsy_api_load_state  (mirror of raw.ga4_load_state)
--    Keyed by (entity, window_key) so each entity's repull window is tracked
--    independently. last_max_created = high-water mark of created_timestamp seen
--    in that window, enabling incremental planning.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.etsy_api_load_state (
    entity            VARCHAR      NOT NULL,   -- 'orders' | 'order_items' | 'listings'
    window_key        VARCHAR      NOT NULL,   -- e.g. '2026-05-27..2026-06-03' or 'full' for listings
    last_max_created  BIGINT,                  -- max created_timestamp (epoch s) seen in this window
    row_count         BIGINT       NOT NULL,
    first_loaded_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_loaded_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    load_count        INTEGER      NOT NULL DEFAULT 1,
    PRIMARY KEY (entity, window_key)
);

CREATE INDEX IF NOT EXISTS etsy_api_load_state_last_loaded_idx
    ON raw.etsy_api_load_state (last_loaded_at DESC);

COMMIT;

-- =============================================================================
-- ROLLBACK (manual; run only to fully reverse this migration)
-- -----------------------------------------------------------------------------
-- BEGIN;
-- DROP TABLE IF EXISTS raw.etsy_api_load_state;
-- DROP INDEX IF EXISTS raw.etsy_order_items_listing_id_num_idx;
-- ALTER TABLE raw.etsy_order_items
--     DROP COLUMN IF EXISTS listing_id_num,
--     DROP COLUMN IF EXISTS created_timestamp,
--     DROP COLUMN IF EXISTS receipt_id,
--     DROP COLUMN IF EXISTS _api_loaded_at;
-- DROP INDEX IF EXISTS raw.etsy_orders_receipt_id_idx;
-- ALTER TABLE raw.etsy_orders
--     DROP COLUMN IF EXISTS receipt_id,
--     DROP COLUMN IF EXISTS created_timestamp,
--     DROP COLUMN IF EXISTS updated_timestamp,
--     DROP COLUMN IF EXISTS is_paid,
--     DROP COLUMN IF EXISTS is_shipped,
--     DROP COLUMN IF EXISTS _api_loaded_at;
-- ALTER TABLE raw.etsy_listings
--     DROP COLUMN IF EXISTS state,
--     DROP COLUMN IF EXISTS created_timestamp,
--     DROP COLUMN IF EXISTS updated_timestamp,
--     DROP COLUMN IF EXISTS url,
--     DROP COLUMN IF EXISTS _api_loaded_at;
-- DROP INDEX IF EXISTS raw.etsy_listings_listing_id_idx;
-- ALTER TABLE raw.etsy_listings DROP COLUMN IF EXISTS listing_id;
-- COMMIT;
-- =============================================================================
