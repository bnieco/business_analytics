-- seed_parts.sql
-- Seeds core.parts and raw.listing_id_map from Etsy listings + order history.
-- Run once against the business DB. Safe to inspect before executing.
-- Proposed SKUs are marked with [PROPOSED] — confirm or override before running.

BEGIN;

-- ---------------------------------------------------------------------------
-- core.parts
-- ---------------------------------------------------------------------------

INSERT INTO core.parts (sku, part_type, name, etsy_listing_id, is_active) VALUES

    -- Daggerheart products
    ('DH-CSB-001',      'good', 'Daggerheart Character Stat Board',                4334519499, true),  -- [PROPOSED SKU] renamed multiple times; canonical listing
    ('DH-FTR-001',      'good', 'Daggerheart Fear Tracker',                        4364365992, true),  -- [PROPOSED SKU]
    ('DH-CPL-001',      'good', 'Core Plus Add-On — Daggerheart Token Deck',       4380084161, true),  -- [PROPOSED SKU]

    -- Wood dice products
    ('DT-VLT-001',      'good', 'Solid Wood Dice Vault & Rolling Tray',            4332433907, true),  -- canonical of DT-WAL-VLT001 / DT-CHR-VLT001
    ('DT-UTL-001',      'good', 'Wood Dice Tray Vault',                            4332484353, true),  -- canonical of DT-WAL-UTL001 / DT-CHR-UTL001
    ('DT-VPR-001',      'good', 'Elder''s Vault of Protection',                    NULL,       true),  -- canonical of DT-CHR-VPR001 / DT-WAL-VPR001; no orders yet
    ('DT-3DP-VPR001',   'good', 'Rolling Tray with Magnetic Lid',                  NULL,       true),  -- existing SKU

    -- Slate coasters (individual designs)
    ('CS-SLT-7DC001',   'good', 'Stone of Protection — 7 Dice',                   NULL,       true),
    ('CS-SLT-SBK002',   'good', 'Stone of Protection — Skull and Books',           NULL,       true),
    ('CS-SLT-FGL003',   'good', 'Stone of Protection — Frog with Lute',            NULL,       true),
    ('CS-SLT-SKS004',   'good', 'Stone of Protection — Skulls',                    NULL,       true),
    ('CS-SLT-MP1005',   'good', 'Stone of Protection — Map 1',                     NULL,       true),
    ('CS-SLT-RKW006',   'good', 'Stone of Protection — Rubik''s World',            NULL,       true),
    ('CS-SLT-SKC007',   'good', 'Stone of Protection — Skull on Compass',          NULL,       true),
    ('CS-SLT-BND001',   'good', 'Bundle of TTRPG Slate Coasters',                  NULL,       true),

    -- Add-ons
    ('ENR-ADD-001',     'component', 'Engrave Your Vault Add-On',                  NULL,       true),  -- [PROPOSED SKU]

    -- Direct/in-person sales
    ('DT-GRD-001',      'good', 'Ground Tray',                                     NULL,       true),  -- [PROPOSED SKU] from direct sale "Ground - 1 Tray"

    -- Custom one-off builds
    ('CW-PLK-001',      'good', 'Custom Solid Wood and Brass Plinko Game',         NULL,       true);  -- [PROPOSED SKU] one-off custom order 4028192890 (2 x $150); listing_id NULL -> resolved by title via DIRECT_ITEM_MAP

-- ---------------------------------------------------------------------------
-- raw.listing_id_map
-- Maps Etsy listing_id → canonical part_id.
-- listing_id=0 (in-person/direct) handled at order_items load time by item_name.
-- ---------------------------------------------------------------------------

INSERT INTO raw.listing_id_map (listing_id, title, sku, part_id, notes)
SELECT
    m.listing_id,
    m.title,
    m.sku,
    p.part_id,
    m.notes
FROM (VALUES
    ('4334519499', 'Daggerheart Character Stat Board (renamed multiple times)', 'DH-CSB-001', 'Confirmed same product across all title variants'),
    ('4332440465', 'Deck of Tokens for Daggerheart (separate listing, same product)', 'DH-CSB-001', 'Director confirmed same canonical part as 4334519499'),
    ('4364365992', 'Daggerheart Fear Tracker', 'DH-FTR-001', NULL),
    ('4380084161', 'Core Plus Add-On — Daggerheart Token Deck', 'DH-CPL-001', NULL),
    ('4332433907', 'Solid Wood Dice Vault & Rolling Tray', 'DT-VLT-001', NULL),
    ('4332484353', 'Wood Dice Tray Vault', 'DT-UTL-001', NULL),
    ('4433369141', 'Custom Vault/Utility Tray Combo', NULL, 'Custom order — no canonical part; exclude from standard reporting')
) AS m(listing_id, title, sku, notes)
LEFT JOIN core.parts p ON p.sku = m.sku;

COMMIT;
