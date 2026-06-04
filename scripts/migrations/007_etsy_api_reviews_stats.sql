-- =============================================================================
-- Migration 007: Etsy Open API v3 — reviews pull + daily listing-stats snapshot
-- Date:     2026-06-04
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Extend the live Etsy API loader (scripts/etsy_api.py) with two new
--           entities (Task #63):
--             (A) reviews     -> raw.etsy_reviews          (existing table, +cols)
--             (B) listing stats snapshot -> raw.etsy_listing_stats_daily (NEW)
--
--   (A) raw.etsy_reviews already holds 35 CSV-era rows (review_id serial PK,
--       order_id bigint, reviewer, reviewed_at, star_rating, message, loaded_at).
--       The Etsy v3 getReviewsByShop response (spec-verified via the Etsy Dev MCP)
--       carries: transaction_id, listing_id, buyer_user_id, rating, review,
--       language, create_timestamp/created_timestamp, update_timestamp/
--       updated_timestamp, shop_id, image_url_fullxfull. It does NOT carry a
--       receipt_id / order_id — a review is keyed on the TRANSACTION. So:
--         * Add nullable API-era columns (transaction_id is the API identity key).
--         * order_id (CSV NOT NULL + UNIQUE) cannot be sourced from a review
--           directly. We best-effort DERIVE it via raw.etsy_order_items
--           (transaction_id -> receipt_id) in the loader, but that map can miss
--           transactions outside our receipts window, and a single receipt can
--           legitimately have >1 reviewed transaction. Both cases violate the
--           CSV-era NOT NULL / UNIQUE on order_id. We therefore RELAX those two
--           constraints (loosening only — never breaks the 35 existing rows, all
--           of which still carry a non-null unique order_id). No CSV-era column is
--           dropped or retyped. Fully reversible (rollback re-adds both).
--
--   (B) raw.etsy_listing_stats_daily — a NEW per-listing daily snapshot of the
--       attention metrics Etsy exposes on getListingsByShop (views, num_favorers)
--       plus state/quantity. Populated from the SAME getListingsByShop responses
--       the loader already fetches (NO new API calls). PK (listing_id,
--       snapshot_date) so a same-day re-run UPSERTs instead of duplicating. This
--       builds the per-listing attention trend series Etsy itself exposes nowhere.
--
-- Design notes:
--   * ADDITIVE + REVERSIBLE. New columns are nullable; the only mutation to an
--     existing column is LOOSENING constraints on order_id (NOT NULL, UNIQUE).
--   * IDEMPOTENT: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS /
--     CREATE INDEX IF NOT EXISTS; constraint drops guarded by IF EXISTS.
--   * The loader keys review idempotency on raw.etsy_reviews.transaction_id
--     (one review per transaction). CSV-era rows have transaction_id IS NULL and
--     MUST survive every refresh.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- (A) raw.etsy_reviews — API-era columns
--     transaction_id is the review's true identity key (one review per txn).
-- -----------------------------------------------------------------------------
ALTER TABLE raw.etsy_reviews
    ADD COLUMN IF NOT EXISTS transaction_id  BIGINT,        -- API identity key (delete-reload key)
    ADD COLUMN IF NOT EXISTS listing_id      BIGINT,        -- review.listing_id
    ADD COLUMN IF NOT EXISTS buyer_user_id   BIGINT,        -- review.buyer_user_id (may be absent per privacy)
    ADD COLUMN IF NOT EXISTS language         VARCHAR,      -- review.language
    ADD COLUMN IF NOT EXISTS updated_at       DATE,         -- from review.update_timestamp (epoch s)
    ADD COLUMN IF NOT EXISTS _api_loaded_at   TIMESTAMPTZ;  -- set by etsy_api.py; NULL on CSV rows

-- Unique index on the API identity key (partial: API rows only). Lets the loader
-- ON CONFLICT / delete-reload by transaction_id without touching CSV rows
-- (transaction_id IS NULL on all 35 CSV-era rows).
CREATE UNIQUE INDEX IF NOT EXISTS etsy_reviews_transaction_id_uidx
    ON raw.etsy_reviews (transaction_id)
    WHERE transaction_id IS NOT NULL;

-- Relax the CSV-era order_id constraints so API rows (which derive order_id
-- best-effort and may share/lack one) can coexist. Loosening only; the 35 CSV
-- rows are unaffected. Constraint names verified live:
--   etsy_reviews_order_id_key (UNIQUE), order_id declared NOT NULL.
ALTER TABLE raw.etsy_reviews
    DROP CONSTRAINT IF EXISTS etsy_reviews_order_id_key;   -- was UNIQUE (order_id)
ALTER TABLE raw.etsy_reviews
    ALTER COLUMN order_id DROP NOT NULL;

-- Keep order_id queryable (it lost its unique index above).
CREATE INDEX IF NOT EXISTS etsy_reviews_order_id_idx
    ON raw.etsy_reviews (order_id)
    WHERE order_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- (B) raw.etsy_listing_stats_daily — NEW daily snapshot
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.etsy_listing_stats_daily (
    listing_id      BIGINT       NOT NULL,
    snapshot_date   DATE         NOT NULL,
    state           TEXT,
    views           BIGINT,
    num_favorers    BIGINT,
    quantity        INTEGER,
    _api_loaded_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (listing_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS etsy_listing_stats_daily_date_idx
    ON raw.etsy_listing_stats_daily (snapshot_date);

COMMIT;

-- =============================================================================
-- ROLLBACK (manual; run only to fully reverse this migration)
-- -----------------------------------------------------------------------------
-- NOTE: re-adding the order_id NOT NULL / UNIQUE will FAIL if any API-era rows
-- with NULL or duplicate order_id were loaded. Clean those first
-- (e.g. DELETE FROM raw.etsy_reviews WHERE transaction_id IS NOT NULL;) to
-- return to the pure CSV-era 35-row state, then run:
--
-- BEGIN;
-- DROP TABLE IF EXISTS raw.etsy_listing_stats_daily;
-- DROP INDEX IF EXISTS raw.etsy_reviews_order_id_idx;
-- ALTER TABLE raw.etsy_reviews ALTER COLUMN order_id SET NOT NULL;
-- ALTER TABLE raw.etsy_reviews ADD CONSTRAINT etsy_reviews_order_id_key UNIQUE (order_id);
-- DROP INDEX IF EXISTS raw.etsy_reviews_transaction_id_uidx;
-- ALTER TABLE raw.etsy_reviews
--     DROP COLUMN IF EXISTS transaction_id,
--     DROP COLUMN IF EXISTS listing_id,
--     DROP COLUMN IF EXISTS buyer_user_id,
--     DROP COLUMN IF EXISTS language,
--     DROP COLUMN IF EXISTS updated_at,
--     DROP COLUMN IF EXISTS _api_loaded_at;
-- COMMIT;
-- =============================================================================
