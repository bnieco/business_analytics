-- =============================================================================
-- Migration 012: inventory.garage_items
-- Date:     2026-07-02
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Garage Inventory App — Postgres home for the garage bin/label
--           inventory previously kept in the hand-maintained spreadsheet
--           `Label Inventory xls format.xls` (Garage Inventory App scope §2).
--
-- Scope of this migration (per the 2026-07-02 green-light decisions):
--   * ONE new table: inventory.garage_items (+ its indexes + one sequence).
--     Nothing else in the `business` DB is touched.
--   * Only the 639 real spreadsheet rows (Index 2-642) are loaded (by the
--     companion loader, garage/load_from_xls.py) — the 1,553 pre-numbered
--     placeholder rows (643-2195) are dropped; they were a spreadsheet-era
--     reservation hack, replaced by a real sequence.
--   * New items mint `idx` from inventory.garage_items_idx_seq, which starts
--     at 643 (Brian confirmed no bins were physically pre-labeled 643+).
--
-- Column mapping (scope §2): every spreadsheet column -> a typed column.
--   Index        -> idx          integer NOT NULL UNIQUE (preserved exactly)
--   Loc Code     -> loc_code     text NOT NULL UNIQUE — STORED, NOT COMPUTED:
--                   the code is physically printed on the bin, so it must not
--                   silently change when room/location is edited. The mint rule
--                   (first(room)+first(location)+idx) lives in the app, at
--                   creation time only. (Scope §2 "store it, don't compute it".)
--   Room         -> room         text (nullable for legacy tolerance)
--   Location     -> location     text (2 legacy rows are legitimately blank)
--   Label Line 1 -> label_line1  text
--   Label Line 2 -> label_line2  text
--   Media        -> media        text
--   Was          -> former_location text  (3 rows; meaning pending Brian)
--   Number       -> qty          integer  (21 rows; meaning pending Brian)
--   Type         -> item_type    text     (85x 'Filament'; meaning pending Brian)
--   (new)        -> status       'active' | 'retired'  (replaces empty-slot hack)
--   (new)        -> search_text  generated: label_line1 + label_line2 + media,
--                   the SOP's three lookup fields, indexed with pg_trgm GIN.
--
-- pg_trgm: CREATE EXTENSION needs superuser. brian_admin IS superuser on
-- postgres16 (verified 2026-07-02), so this runs clean here; on a less
-- privileged role the DO block below degrades to skipping the trigram index
-- (ILIKE still works, just unindexed) and raises a NOTICE instead of failing.
--
-- Idempotent: safe to re-run.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS inventory;

-- Trigram extension for the fuzzy lookup index. Degrade gracefully if the
-- running role can't install extensions.
DO $$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'pg_trgm not installable by current role; trigram index will be skipped.';
    END;
END$$;

CREATE TABLE IF NOT EXISTS inventory.garage_items (
    id              bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idx             integer NOT NULL UNIQUE,
    loc_code        text    NOT NULL UNIQUE,
    room            text,
    location        text,
    label_line1     text,
    label_line2     text,
    media           text,
    former_location text,
    qty             integer,
    item_type       text,
    status          text    NOT NULL DEFAULT 'active',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    search_text     text GENERATED ALWAYS AS (
        coalesce(label_line1, '') || ' ' ||
        coalesce(label_line2, '') || ' ' ||
        coalesce(media, '')
    ) STORED
);

-- Status vocabulary (scope §2.1). Idempotent add.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'garage_items_status_chk'
          AND conrelid = 'inventory.garage_items'::regclass
    ) THEN
        ALTER TABLE inventory.garage_items
            ADD CONSTRAINT garage_items_status_chk
            CHECK (status IN ('active', 'reserved', 'retired'));
    END IF;
END$$;

-- The numeric tail for NEW loc codes. Starts at 643: real rows end at 642 and
-- Brian confirmed the 643-2195 placeholders were never physically labeled, so
-- the reservation model is retired (scope §2.1 recommended path, decided
-- 2026-07-02). START WITH only applies at creation; re-runs are no-ops.
CREATE SEQUENCE IF NOT EXISTS inventory.garage_items_idx_seq
    AS integer
    START WITH 643
    OWNED BY inventory.garage_items.idx;

-- Trigram GIN index over the SOP's three lookup fields (via search_text).
-- Skipped with a NOTICE if pg_trgm could not be installed above.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = 'idx_garage_items_search_trgm'
              AND n.nspname = 'inventory'
        ) THEN
            CREATE INDEX idx_garage_items_search_trgm
                ON inventory.garage_items
                USING gin (search_text gin_trgm_ops);
        END IF;
    ELSE
        RAISE NOTICE 'pg_trgm missing; idx_garage_items_search_trgm not created.';
    END IF;
END$$;

-- Plain btree helpers for exact/prefix loc-code lookups and room browsing.
-- (idx and loc_code already have UNIQUE btree indexes from their constraints.)
CREATE INDEX IF NOT EXISTS idx_garage_items_room
    ON inventory.garage_items (room);

COMMENT ON TABLE inventory.garage_items IS
    'Garage/office/kitchen bin inventory (Garage Inventory App). Migrated 2026-07-02 from "Label Inventory xls format.xls" — only the 639 real rows; placeholder rows 643-2195 dropped, replaced by garage_items_idx_seq (starts 643). loc_code is printed on the physical bin: stored at creation, never recomputed.';
COMMENT ON COLUMN inventory.garage_items.idx IS
    'Legacy sequential integer (spreadsheet "Index"); numeric tail of loc_code. Preserved exactly on migration; new values from inventory.garage_items_idx_seq.';
COMMENT ON COLUMN inventory.garage_items.loc_code IS
    'Physical locator printed on the bin: first(room)+first(location)+idx, minted by the app at creation. STORED — never recomputed on edit (the printed label does not reprint itself).';
COMMENT ON COLUMN inventory.garage_items.former_location IS
    'Spreadsheet "Was" (3 rows). Meaning unconfirmed — pending Brian (scope §7.2).';
COMMENT ON COLUMN inventory.garage_items.qty IS
    'Spreadsheet "Number" (21 rows, small ints). Meaning unconfirmed — pending Brian (scope §7.2).';
COMMENT ON COLUMN inventory.garage_items.item_type IS
    'Spreadsheet "Type" (85 rows, all ''Filament''). Meaning unconfirmed — pending Brian (scope §7.2).';
COMMENT ON COLUMN inventory.garage_items.search_text IS
    'Generated: label_line1 + label_line2 + media — the SOP''s three lookup fields; backed by the pg_trgm GIN index.';
