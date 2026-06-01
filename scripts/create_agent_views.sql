-- create_agent_views.sql
-- Agent-queryable product views for marketing, listing, and product strategy work.
-- Single query from either view is sufficient to bootstrap agent context.

BEGIN;

-- ---------------------------------------------------------------------------
-- ops.product_catalog
-- Rich product context: listing content joined to canonical parts + sales summary.
-- Join strategy: exact SKU match preferred; title-prefix fallback for NaN-SKU
-- and multi-material listings (e.g. 'DT-WAL-VLT001,DT-CHR-VLT001').
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW ops.product_catalog AS
WITH listing_matched AS (
    SELECT
        p.part_id,
        el.title            AS listing_title,
        el.description,
        el.price            AS list_price,
        el.tags,
        el.materials,
        el.variation_1_type,
        el.variation_1_name,
        el.variation_1_values,
        el.variation_2_type,
        el.variation_2_name,
        el.variation_2_values,
        el.image1, el.image2, el.image3, el.image4, el.image5,
        el.image6, el.image7, el.image8, el.image9, el.image10,
        ROW_NUMBER() OVER (
            PARTITION BY p.part_id
            ORDER BY CASE WHEN el.sku = p.sku THEN 0 ELSE 1 END  -- prefer exact SKU match
        ) AS rn
    FROM core.parts p
    LEFT JOIN raw.etsy_listings el ON (
        el.sku = p.sku
        OR replace(lower(el.title), e'’', '''') LIKE lower(left(p.name, 15)) || '%'
    )
),
sales AS (
    SELECT
        oi.part_id,
        SUM(oi.quantity)        AS units_sold,
        COUNT(DISTINCT o.order_id) AS order_count,
        SUM(oi.line_total)      AS gross_revenue,
        MIN(o.ordered_at)       AS first_sale_date,
        MAX(o.ordered_at)       AS last_sale_date
    FROM core.order_items oi
    JOIN core.orders o ON o.order_id = oi.order_id
    GROUP BY oi.part_id
)
SELECT
    p.part_id,
    p.sku,
    p.part_type,
    p.name,
    p.is_active,
    p.etsy_listing_id,
    -- Listing content (null if no listing export row matched)
    lm.listing_title,
    lm.description,
    lm.list_price,
    lm.tags,
    lm.materials,
    lm.variation_1_type,
    lm.variation_1_name,
    lm.variation_1_values,
    lm.variation_2_type,
    lm.variation_2_name,
    lm.variation_2_values,
    lm.image1, lm.image2, lm.image3, lm.image4, lm.image5,
    lm.image6, lm.image7, lm.image8, lm.image9, lm.image10,
    -- Sales history
    COALESCE(s.units_sold,    0) AS units_sold,
    COALESCE(s.order_count,   0) AS order_count,
    COALESCE(s.gross_revenue, 0) AS gross_revenue,
    s.first_sale_date,
    s.last_sale_date
FROM core.parts p
LEFT JOIN listing_matched lm ON lm.part_id = p.part_id AND lm.rn = 1
LEFT JOIN sales s ON s.part_id = p.part_id
WHERE p.deleted_at IS NULL
ORDER BY COALESCE(s.units_sold, 0) DESC, p.sku;

COMMENT ON VIEW ops.product_catalog IS
    'Agent-queryable product catalog. Joins canonical parts to Etsy listing content (description, tags, materials, variants, images) and sales history. One row per part. Intended as the primary context source for marketing, listing, and product strategy agents.';


-- ---------------------------------------------------------------------------
-- ops.product_summary
-- Compact overview for quick agent bootstrapping — no description/image columns.
-- Use this for broad product awareness; use product_catalog for deep context.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW ops.product_summary AS
SELECT
    p.part_id,
    p.sku,
    p.name,
    p.part_type,
    p.is_active,
    lm.list_price,
    lm.tags,
    lm.materials,
    COALESCE(s.units_sold,    0) AS units_sold,
    COALESCE(s.gross_revenue, 0) AS gross_revenue,
    s.first_sale_date,
    s.last_sale_date,
    CASE
        WHEN p.etsy_listing_id IS NOT NULL THEN 'active_listing'
        WHEN s.units_sold > 0              THEN 'sold_no_listing'
        ELSE                                    'listing_only'
    END AS product_status
FROM core.parts p
LEFT JOIN (
    SELECT
        p2.part_id,
        el.list_price,
        el.tags,
        el.materials,
        ROW_NUMBER() OVER (PARTITION BY p2.part_id ORDER BY CASE WHEN el.sku = p2.sku THEN 0 ELSE 1 END) AS rn
    FROM core.parts p2
    LEFT JOIN (SELECT sku, price AS list_price, tags, materials, title FROM raw.etsy_listings) el
        ON el.sku = p2.sku OR lower(el.title) LIKE lower(left(p2.name, 20)) || '%'
) lm ON lm.part_id = p.part_id AND lm.rn = 1
LEFT JOIN (
    SELECT part_id, SUM(quantity) AS units_sold, SUM(line_total) AS gross_revenue,
           MIN(o.ordered_at) AS first_sale_date, MAX(o.ordered_at) AS last_sale_date
    FROM core.order_items oi
    JOIN core.orders o ON o.order_id = oi.order_id
    GROUP BY oi.part_id
) s ON s.part_id = p.part_id
WHERE p.deleted_at IS NULL
ORDER BY COALESCE(s.units_sold, 0) DESC, p.sku;

COMMENT ON VIEW ops.product_summary IS
    'Compact product overview for quick agent bootstrapping. No description or image columns. One row per part with price, tags, materials, and sales totals.';

COMMIT;
