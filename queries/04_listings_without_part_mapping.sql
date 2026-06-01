-- Etsy listings seen in raw order items but not yet mapped to a canonical part.
-- Director reviews these and updates raw.listing_id_map.part_id.
SELECT
    roi.listing_id,
    MIN(roi.item_name)                          AS sample_item_name,
    COUNT(*)                                    AS order_item_rows,
    SUM(roi.quantity::int)                      AS total_units_sold,
    MIN(roi.sale_date)                          AS first_sale,
    MAX(roi.sale_date)                          AS last_sale
FROM raw.etsy_order_items roi
LEFT JOIN raw.listing_id_map m ON m.listing_id = roi.listing_id
WHERE roi.listing_id IS NOT NULL
  AND (m.listing_id IS NULL OR m.part_id IS NULL)
GROUP BY roi.listing_id
ORDER BY total_units_sold DESC NULLS LAST;
