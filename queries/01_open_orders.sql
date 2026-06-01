-- Open (unfulfilled) orders, newest first.
SELECT
    o.order_id,
    o.source,
    o.source_order_id,
    c.display_name                          AS customer,
    o.ordered_at,
    o.total,
    COUNT(oi.order_item_id)                 AS line_count,
    SUM(oi.quantity)                        AS total_units
FROM core.orders o
JOIN core.customers  c  ON c.customer_id = o.customer_id
LEFT JOIN core.order_items oi ON oi.order_id = o.order_id
WHERE o.status = 'open'
GROUP BY o.order_id, o.source, o.source_order_id, c.display_name, o.ordered_at, o.total
ORDER BY o.ordered_at DESC;
