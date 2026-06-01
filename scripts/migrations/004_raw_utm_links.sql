-- =============================================================================
-- Migration 004: raw.utm_links — tagged-URL registry
-- Date:     2026-05-23
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Replace Brian's manual `UTM_Tracker_Compat.xlsx` with a queryable
--           Postgres table. Every URL produced by `scripts/utm.py` auto-logs a
--           row here; historical xlsx rows can be back-filled with
--           `created_by = 'xlsx_import'`.
--
-- Design notes:
--   * `url_full` is the natural key — UNIQUE so we get cheap upserts.
--     ON CONFLICT DO NOTHING preserves the first-creation timestamp.
--   * utm_* params are stored as their own columns so joins to
--     `raw.ga4_events.collected_traffic_source_*` are trivial.
--   * `generated_count` is bumped on re-generation so Brian can see which
--     tagged URLs he keeps re-creating (often a sign of a chronic post).
--   * `utm_term` and `utm_id` are not in Phase 1's scheme but the xlsx had
--     a `term` column. Including utm_term as nullable for future-proofing
--     and historical back-fill; utm_id is skipped (xlsx values were 1001…).
--
-- Idempotent: safe to re-run.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.utm_links (
    id              BIGSERIAL    PRIMARY KEY,

    -- The full tagged URL — natural key
    url_full        TEXT         NOT NULL,

    -- Constituent parts (denormalised for query convenience)
    base_url        TEXT         NOT NULL,
    utm_source      TEXT         NOT NULL,
    utm_medium      TEXT         NOT NULL,
    utm_campaign    TEXT         NOT NULL,
    utm_content     TEXT,
    utm_term        TEXT,                              -- not part of Phase 1, but kept for back-fill

    -- Free-form annotations
    notes           TEXT,                              -- Brian can UPDATE via SQL
    campaign_name   TEXT,                              -- xlsx "Campaign Name" — often == utm_campaign
    short_link      TEXT,                              -- etsy.me, bit.ly, etc.
    coupon          TEXT,                              -- e.g. PINTFEATURES

    -- Provenance / lifecycle
    created_by      TEXT         NOT NULL DEFAULT 'utm.py',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    generated_count INT          NOT NULL DEFAULT 1,

    CONSTRAINT utm_links_url_full_uq UNIQUE (url_full)
);

-- Indexes -----------------------------------------------------------------------
-- Phase 1 dashboard joins on (source, campaign, content); index that triplet.
CREATE INDEX IF NOT EXISTS utm_links_attribution_idx
    ON raw.utm_links (utm_source, utm_campaign, utm_content);

CREATE INDEX IF NOT EXISTS utm_links_campaign_idx
    ON raw.utm_links (utm_campaign);

CREATE INDEX IF NOT EXISTS utm_links_created_at_idx
    ON raw.utm_links (created_at DESC);
