-- ROLLBACK ONLY — discards all parent associations recorded since 013 was applied.
-- Rollback for migration 013. Restores the pre-013 schema exactly.
-- WARNING: discards every parent association recorded since 013 was applied.
DROP TRIGGER IF EXISTS trg_garage_items_no_cycle ON inventory.garage_items;
DROP FUNCTION IF EXISTS inventory.garage_items_no_cycle();
DROP INDEX IF EXISTS inventory.idx_garage_items_parent;
ALTER TABLE inventory.garage_items
    DROP CONSTRAINT IF EXISTS garage_items_no_self_parent;
ALTER TABLE inventory.garage_items
    DROP COLUMN IF EXISTS parent_idx;   -- drops garage_items_parent_fk with it
