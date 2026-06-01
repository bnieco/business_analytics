-- =============================================================================
-- Migration 001: raw GA4 events landing tables
-- Date:     2026-05-22
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Create landing tables for GA4 BigQuery export data in the shared
--           `raw` schema of the `business` DB.
--
--   raw.ga4_events     — one row per GA4 event, mirroring BigQuery's
--                        events_YYYYMMDD grain. High-value scalar fields are
--                        promoted to columns for cheap filtering / joins;
--                        nested structures (event_params, items,
--                        user_properties) are stored as jsonb.
--
--   raw.ga4_load_state — per-event-date watermark + load metadata so the
--                        loader can resume incrementally and honour the
--                        3-day re-pull window.
--
-- Idempotent: safe to re-run.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS raw;

-- -----------------------------------------------------------------------------
-- raw.ga4_events
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.ga4_events (
    -- Surrogate PK
    ga4_event_id            BIGSERIAL    PRIMARY KEY,

    -- Partition / time fields
    event_date              DATE         NOT NULL,
    event_timestamp         BIGINT       NOT NULL,        -- microseconds since epoch (GA4 native)
    event_name              TEXT         NOT NULL,
    event_bundle_sequence_id BIGINT,

    -- User / session identifiers
    user_pseudo_id          TEXT,
    user_id                 TEXT,
    ga_session_id           BIGINT,                       -- extracted from event_params.ga_session_id
    ga_session_number       BIGINT,                       -- extracted from event_params.ga_session_number

    -- Page context (extracted from event_params)
    page_location           TEXT,
    page_title              TEXT,
    page_referrer           TEXT,

    -- Listing extracted from page_location (ENR-specific convenience column)
    listing_id              BIGINT,

    -- Device
    device_category         TEXT,
    device_mobile_brand     TEXT,
    device_mobile_model     TEXT,
    device_operating_system TEXT,
    device_browser          TEXT,
    device_language         TEXT,

    -- Geo
    geo_country             TEXT,
    geo_region              TEXT,
    geo_city                TEXT,
    geo_continent           TEXT,

    -- Traffic source (event-level traffic_source.*)
    traffic_source_name     TEXT,
    traffic_source_medium   TEXT,
    traffic_source_source   TEXT,

    -- Collected traffic source (session-scoped, also extracted from params)
    collected_traffic_source_source   TEXT,
    collected_traffic_source_medium   TEXT,
    collected_traffic_source_campaign TEXT,

    -- Nested structures preserved as jsonb for downstream unnesting
    event_params            JSONB,
    items                   JSONB,
    user_properties         JSONB,

    -- Load metadata
    bq_export_table         TEXT         NOT NULL,        -- e.g. events_20260522
    loaded_at               TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Indexes -----------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ga4_events_event_date_idx
    ON raw.ga4_events (event_date);

CREATE INDEX IF NOT EXISTS ga4_events_event_name_idx
    ON raw.ga4_events (event_name);

CREATE INDEX IF NOT EXISTS ga4_events_user_pseudo_id_idx
    ON raw.ga4_events (user_pseudo_id);

CREATE INDEX IF NOT EXISTS ga4_events_ga_session_id_idx
    ON raw.ga4_events (ga_session_id);

CREATE INDEX IF NOT EXISTS ga4_events_listing_id_idx
    ON raw.ga4_events (listing_id) WHERE listing_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ga4_events_date_name_idx
    ON raw.ga4_events (event_date, event_name);


-- -----------------------------------------------------------------------------
-- raw.ga4_load_state
-- One row per event_date that has been loaded. Tracks the BigQuery source
-- table, row count, and load timestamp so the loader can:
--   * resume from the most-recent watermark
--   * honour the 3-day re-pull window (re-load the last N dates each run)
--   * detect schema/version drift via bq_export_table
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.ga4_load_state (
    event_date       DATE         PRIMARY KEY,
    bq_export_table  TEXT         NOT NULL,           -- e.g. events_20260522 or events_intraday_20260522
    row_count        BIGINT       NOT NULL,
    first_loaded_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_loaded_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    load_count       INT          NOT NULL DEFAULT 1   -- bumped on every re-pull
);

CREATE INDEX IF NOT EXISTS ga4_load_state_last_loaded_idx
    ON raw.ga4_load_state (last_loaded_at DESC);
