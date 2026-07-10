-- =============================================================================
-- Migration 013: inventory.garage_items.parent_idx  (parent/child hierarchy)
-- Additive + reversible (down: 013_garage_items_parent_down.sql).
-- Existing rows untouched; new column is NULL for all of them.
-- Idempotent: safe to re-run.
-- =============================================================================

-- 1) The association column: adjacency list on the app key `idx`.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'inventory' AND table_name = 'garage_items'
          AND column_name = 'parent_idx'
    ) THEN
        ALTER TABLE inventory.garage_items
            ADD COLUMN parent_idx integer
                CONSTRAINT garage_items_parent_fk
                REFERENCES inventory.garage_items(idx)
                ON DELETE RESTRICT;
    END IF;
END$$;

-- 2) Self-parenting guard (cheap DB-level backstop; cycles handled below).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'garage_items_no_self_parent'
          AND conrelid = 'inventory.garage_items'::regclass
    ) THEN
        ALTER TABLE inventory.garage_items
            ADD CONSTRAINT garage_items_no_self_parent
            CHECK (parent_idx IS DISTINCT FROM idx);
    END IF;
END$$;

-- 3) Direct-children lookups: partial index (most rows will stay NULL).
CREATE INDEX IF NOT EXISTS idx_garage_items_parent
    ON inventory.garage_items (parent_idx)
    WHERE parent_idx IS NOT NULL;

-- 4) Cycle prevention: an item may never become its own ancestor.
--    Raises check_violation (23514) so psycopg2 surfaces CheckViolation,
--    the same class as the self-parent CHECK — one except branch in the app.
CREATE OR REPLACE FUNCTION inventory.garage_items_no_cycle()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    cur   integer := NEW.parent_idx;
    depth integer := 0;
BEGIN
    WHILE cur IS NOT NULL LOOP
        IF cur = NEW.idx THEN
            RAISE EXCEPTION USING
                errcode = 'check_violation',
                message = format(
                    'parent cycle: item idx %s would become its own ancestor',
                    NEW.idx);
        END IF;
        depth := depth + 1;
        IF depth > 100 THEN
            RAISE EXCEPTION USING
                errcode = 'check_violation',
                message = 'parent chain deeper than 100 levels — aborting';
        END IF;
        SELECT g.parent_idx INTO cur
        FROM inventory.garage_items g WHERE g.idx = cur;
    END LOOP;
    RETURN NEW;
END $fn$;

DROP TRIGGER IF EXISTS trg_garage_items_no_cycle ON inventory.garage_items;
CREATE TRIGGER trg_garage_items_no_cycle
    BEFORE INSERT OR UPDATE OF parent_idx ON inventory.garage_items
    FOR EACH ROW WHEN (NEW.parent_idx IS NOT NULL)
    EXECUTE FUNCTION inventory.garage_items_no_cycle();

COMMENT ON COLUMN inventory.garage_items.parent_idx IS
    'Adjacency-list parent (references idx, the app key). NULL = top-level. Direct children = WHERE parent_idx = <idx>. Cycles blocked by trg_garage_items_no_cycle; self-parent by garage_items_no_self_parent; ON DELETE RESTRICT (re-parent children before any manual delete).';
