-- 005_ops_product_summary_display_label.sql
-- Adds display_label column to ops.product_summary as the canonical
-- product-reference string for personas: "<name> (SKU: <sku>)".
--
-- Background: SKU-vs-name audit (2026-05-24) flagged repeated cases of personas
-- using bare SKU strings ("DT-VLT-001") as product names in human-facing prose.
-- display_label gives personas a single, ready-made string to pull when they
-- need to refer to a product without conflating the two identifiers.
--
-- Note: ops.product_summary is a VIEW; modify via CREATE OR REPLACE VIEW.
-- New column appended after product_status to avoid breaking column order
-- for any consumers selecting by position.
--
-- Owner: Edison (Analytics Engineer)
-- Date:  2026-05-24

CREATE OR REPLACE VIEW ops.product_summary AS
 SELECT p.part_id,
    p.sku,
    p.name,
    p.part_type,
    p.is_active,
    lm.list_price,
    lm.tags,
    lm.materials,
    COALESCE(s.units_sold, 0::bigint) AS units_sold,
    COALESCE(s.gross_revenue, 0::numeric) AS gross_revenue,
    s.first_sale_date,
    s.last_sale_date,
        CASE
            WHEN p.etsy_listing_id IS NOT NULL THEN 'active_listing'::text
            WHEN s.units_sold > 0 THEN 'sold_no_listing'::text
            ELSE 'listing_only'::text
        END AS product_status,
    (p.name || ' (SKU: '::text || p.sku || ')'::text) AS display_label
   FROM core.parts p
     LEFT JOIN ( SELECT p2.part_id,
            el.list_price,
            el.tags,
            el.materials,
            row_number() OVER (PARTITION BY p2.part_id ORDER BY (
                CASE
                    WHEN el.sku::text = p2.sku THEN 0
                    ELSE 1
                END)) AS rn
           FROM core.parts p2
             LEFT JOIN ( SELECT etsy_listings.sku,
                    etsy_listings.price AS list_price,
                    etsy_listings.tags,
                    etsy_listings.materials,
                    etsy_listings.title
                   FROM raw.etsy_listings) el ON el.sku::text = p2.sku OR lower(el.title) ~~ (lower("left"(p2.name, 20)) || '%'::text)) lm ON lm.part_id = p.part_id AND lm.rn = 1
     LEFT JOIN ( SELECT oi.part_id,
            sum(oi.quantity) AS units_sold,
            sum(oi.line_total) AS gross_revenue,
            min(o.ordered_at) AS first_sale_date,
            max(o.ordered_at) AS last_sale_date
           FROM core.order_items oi
             JOIN core.orders o ON o.order_id = oi.order_id
          GROUP BY oi.part_id) s ON s.part_id = p.part_id
  WHERE p.deleted_at IS NULL
  ORDER BY (COALESCE(s.units_sold, 0::bigint)) DESC, p.sku;

COMMENT ON COLUMN ops.product_summary.display_label IS
  'Canonical product-reference string for human-facing prose: "<name> (SKU: <sku>)". Added 2026-05-24 per SKU-vs-name audit.';
