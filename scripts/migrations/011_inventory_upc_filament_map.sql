-- =============================================================================
-- Migration 011: inventory.upc_filament_map
-- Date:     2026-06-30
-- Author:   Edison (Analytics Engineer)
-- Purpose:  ENR-owned UPC/EAN -> Spoolman filament mapping table for the
--           Filament Inventory System (Phase 2 box-scan intake layer).
--
--   inventory.upc_filament_map — the single source of truth for the
--           barcode <-> Spoolman filament binding. One row per distinct
--           manufacturer box barcode (UPC/EAN). Lives in the `business` DB
--           (ENR institutional data) rather than inside Spoolman's schema;
--           the copy written to Spoolman's filament `extra.barcode` is an
--           advisory/denormalized convenience copy, NOT co-equal. If the two
--           ever disagree, this table wins. (Scope §4.)
--
-- Data model per Scope §4:
--   barcode               text, PK       The scanned UPC/EAN (key / SSOT).
--   spoolman_filament_id  integer        The Spoolman filament this represents.
--   brand                 text           Denormalized brand label (human review/seeding).
--   material              text           Denormalized type (human review/seeding).
--   color                 text           Denormalized color (human review/seeding).
--   source                text           How added: 'scan_bind' (Option 3) | 'seed' (imported).
--   created_at            timestamptz    When the binding was made.
--
-- Idempotent: safe to re-run.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS inventory;

CREATE TABLE IF NOT EXISTS inventory.upc_filament_map (
    barcode               text        PRIMARY KEY,
    spoolman_filament_id  integer     NOT NULL,
    brand                 text,
    material              text,
    color                 text,
    source                text        NOT NULL DEFAULT 'scan_bind',
    created_at            timestamptz NOT NULL DEFAULT now()
);

-- Constrain `source` to the two known provenances (Scope §4). Added
-- idempotently so re-runs on an existing table do not error.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'upc_filament_map_source_chk'
          AND conrelid = 'inventory.upc_filament_map'::regclass
    ) THEN
        ALTER TABLE inventory.upc_filament_map
            ADD CONSTRAINT upc_filament_map_source_chk
            CHECK (source IN ('scan_bind', 'seed'));
    END IF;
END$$;

-- Secondary lookup: "which barcode(s) point at this Spoolman filament" — used
-- when rebuilding / reconciling the map against Spoolman.
CREATE INDEX IF NOT EXISTS idx_upc_filament_map_filament_id
    ON inventory.upc_filament_map (spoolman_filament_id);

COMMENT ON TABLE inventory.upc_filament_map IS
    'SSOT for manufacturer box barcode (UPC/EAN) -> Spoolman filament_id binding. Phase 2 box-scan intake. Spoolman extra.barcode is an advisory copy; this table wins on conflict. (Filament Inventory System scope §4.)';
COMMENT ON COLUMN inventory.upc_filament_map.barcode IS 'Scanned UPC/EAN; primary key / single source of truth.';
COMMENT ON COLUMN inventory.upc_filament_map.spoolman_filament_id IS 'Spoolman filament id this barcode resolves to.';
COMMENT ON COLUMN inventory.upc_filament_map.source IS 'Provenance: scan_bind (Option 3 first-encounter) | seed (bulk import).';
