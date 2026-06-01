-- Customer roll-up across Etsy + Direct.
SELECT
    c.customer_id,
    c.source,
    c.display_name,
    c.email,
    c.first_seen_at,
    c.last_order_at,
    COUNT(o.order_id)                             AS orders,
    COALESCE(SUM(o.total), 0)                     AS lifetime_revenue,
    COALESCE(SUM(o.total) FILTER (WHERE o.status = 'fulfilled'), 0) AS fulfilled_revenue
FROM core.customers c
LEFT JOIN core.orders o ON o.customer_id = c.customer_id
WHERE c.deleted_at IS NULL
GROUP BY c.customer_id, c.source, c.display_name, c.email, c.first_seen_at, c.last_order_at
ORDER BY lifetime_revenue DESC;
