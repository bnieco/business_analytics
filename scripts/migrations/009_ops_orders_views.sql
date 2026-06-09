-- =============================================================================
-- Migration 009: ops views for Etsy orders (Brian request — dashboard Orders section)
-- Date:     2026-06-09
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Surface Etsy order data (raw.etsy_orders + raw.etsy_order_items,
--           API-fed via the daily 06:15 Etsy ingest) on the live phase1-dashboard
--           as a front-and-center "Orders" section, following the migration-008
--           convention (CREATE OR REPLACE VIEW + COMMENT ON VIEW, owned by
--           brian_admin, GRANT SELECT to the read roles forge_ops + mcp_readonly).
--
--   (1) ops.orders_rollup       — single-row scoreboard: this-year (2026 YTD)
--       and this-ISO-week order counts + revenue (SUM(order_total)), plus the
--       exact ISO-week date range and a freshness stamp (max sale_date / max
--       _api_loaded_at) so a missing same-day order reads as "not ingested yet",
--       not "no sale".
--
--   (2) ops.orders_week_detail  — this-ISO-week line items ("what was ordered"):
--       date, product title, qty, unit price, order_id. One row per line item;
--       the renderer groups by order_id so a multi-item order reads as one order.
--
--   (3) ops.orders_recent       — line items for the last 14 days, with a
--       canceled/refunded-excluded flag carried per row. The dashboard reads
--       this for BOTH "yesterday" (filter sale_date = CURRENT_DATE - 1 in the
--       renderer) and as a small recent-window source; keeping one view avoids
--       a near-duplicate. Ordered newest-first.
--
-- Order definition / status filter:
--   An "order" excludes canceled / fully-refunded receipts. Observed distinct
--   raw.etsy_orders.status values (2026-06-09): 'Completed', 'Paid',
--   'Partially Refunded'. None are canceled/fully-refunded today, but the filter
--   is written defensively against the canonical Etsy status strings so a future
--   cancellation is excluded automatically:
--       status NOT IN ('Canceled','Cancelled','Refunded','Fully Refunded')
--   NULL status is KEPT (treated as a live order) so an un-stamped API row is
--   never silently dropped from the count. 'Partially Refunded' COUNTS as an
--   order (the buyer kept part of it) — only fully-canceled/refunded drop out.
--
-- Revenue note:
--   Rollup revenue = SUM(order_total) (gross order value incl. shipping + tax),
--   matching what Brian sees as the order's headline total on Etsy. The line-item
--   `price` in orders_week_detail is the per-unit item price (ex shipping/tax),
--   so a one-item order's detail price can be < its order_total. Both are correct;
--   they measure different things.
--
-- Design notes:
--   * Views only — NO writes to raw.*. Fully reversible (DROP VIEW at bottom).
--   * "This year" = EXTRACT(year FROM sale_date) = EXTRACT(year FROM CURRENT_DATE).
--   * "This week" = current ISO week (Mon-Sun): sale_date in
--     [date_trunc('week', CURRENT_DATE), +7 days). Postgres date_trunc('week')
--     is Monday-anchored, so this is the ISO Mon-Sun week.
--   * Item title = raw.etsy_order_items.item_name (the as-ordered title);
--     falls back to the current listing title (raw.etsy_listings) then a
--     '#<listing_id>' label so a row never renders blank.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- (1) ops.orders_rollup — one-row scoreboard (YTD + this-ISO-week) + freshness.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ops.orders_rollup AS
WITH live AS (
    SELECT *
    FROM raw.etsy_orders
    WHERE status IS NULL
       OR status NOT IN ('Canceled', 'Cancelled', 'Refunded', 'Fully Refunded')
),
bounds AS (
    SELECT
        EXTRACT(year FROM CURRENT_DATE)::int       AS this_year,
        date_trunc('week', CURRENT_DATE)::date      AS week_start,
        (date_trunc('week', CURRENT_DATE) + INTERVAL '6 days')::date AS week_end
)
SELECT
    b.this_year,
    b.week_start,
    b.week_end,
    -- YTD (this calendar year)
    COUNT(*) FILTER (
        WHERE EXTRACT(year FROM o.sale_date) = b.this_year
    )                                                           AS ytd_orders,
    COALESCE(SUM(o.order_total) FILTER (
        WHERE EXTRACT(year FROM o.sale_date) = b.this_year
    ), 0)                                                       AS ytd_revenue,
    -- This ISO week (Mon-Sun)
    COUNT(*) FILTER (
        WHERE o.sale_date >= b.week_start AND o.sale_date <= b.week_end
    )                                                           AS week_orders,
    COALESCE(SUM(o.order_total) FILTER (
        WHERE o.sale_date >= b.week_start AND o.sale_date <= b.week_end
    ), 0)                                                       AS week_revenue,
    -- Freshness — orders are as-of the daily Etsy ingest; today's order won't
    -- appear until tomorrow's run. Surface both stamps so the panel can say
    -- "orders current through …".
    MAX(o.sale_date)                                            AS max_sale_date,
    MAX(o._api_loaded_at)                                       AS max_api_loaded_at
FROM live o
CROSS JOIN bounds b
GROUP BY b.this_year, b.week_start, b.week_end;

COMMENT ON VIEW ops.orders_rollup IS
    'One-row Etsy orders scoreboard (Brian request, 2026-06-09): this-year (YTD) + current-ISO-week order counts and revenue (SUM(order_total)), the ISO week_start/week_end (Mon-Sun), and freshness stamps (max sale_date / max _api_loaded_at). Excludes Canceled/Cancelled/Refunded/Fully Refunded; keeps Partially Refunded + NULL status. Dashboard Orders section + agent context.';

-- -----------------------------------------------------------------------------
-- (2) ops.orders_week_detail — this-ISO-week line items ("what was ordered").
--     One row per line item; renderer groups by order_id.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ops.orders_week_detail AS
SELECT
    i.sale_date,
    i.order_id,
    COALESCE(
        NULLIF(TRIM(i.item_name), ''),
        l.title,
        '#' || i.listing_id
    )                                                           AS item_title,
    i.quantity,
    i.price                                                     AS unit_price,
    i.listing_id,
    o.order_total
FROM raw.etsy_order_items i
JOIN raw.etsy_orders o
    ON o.order_id = i.order_id
LEFT JOIN raw.etsy_listings l
    ON l.listing_id = i.listing_id_num
   AND l.listing_id IS NOT NULL
WHERE i.sale_date >= date_trunc('week', CURRENT_DATE)::date
  AND i.sale_date <= (date_trunc('week', CURRENT_DATE) + INTERVAL '6 days')::date
  AND (o.status IS NULL
       OR o.status NOT IN ('Canceled', 'Cancelled', 'Refunded', 'Fully Refunded'))
ORDER BY i.sale_date DESC, i.order_id, item_title;

COMMENT ON VIEW ops.orders_week_detail IS
    'This-ISO-week (Mon-Sun) Etsy order line items (Brian request, 2026-06-09): sale_date, order_id, item_title (item_name, falling back to current listing title then #listing_id), quantity, unit_price, plus the parent order_total. One row per line item; dashboard groups by order_id so a multi-item order reads as one order. Same status filter as ops.orders_rollup.';

-- -----------------------------------------------------------------------------
-- (3) ops.orders_recent — last-14-day line items (newest first). Dashboard reads
--     this for the "yesterday" callout (sale_date = CURRENT_DATE - 1) and as a
--     short recent window. Single view avoids a near-duplicate of week_detail.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ops.orders_recent AS
SELECT
    i.sale_date,
    i.order_id,
    COALESCE(
        NULLIF(TRIM(i.item_name), ''),
        l.title,
        '#' || i.listing_id
    )                                                           AS item_title,
    i.quantity,
    i.price                                                     AS unit_price,
    i.listing_id,
    o.order_total,
    o.status
FROM raw.etsy_order_items i
JOIN raw.etsy_orders o
    ON o.order_id = i.order_id
LEFT JOIN raw.etsy_listings l
    ON l.listing_id = i.listing_id_num
   AND l.listing_id IS NOT NULL
WHERE i.sale_date >= CURRENT_DATE - INTERVAL '14 days'
  AND (o.status IS NULL
       OR o.status NOT IN ('Canceled', 'Cancelled', 'Refunded', 'Fully Refunded'))
ORDER BY i.sale_date DESC, i.order_id, item_title;

COMMENT ON VIEW ops.orders_recent IS
    'Last-14-day Etsy order line items (Brian request, 2026-06-09), newest first: sale_date, order_id, item_title, quantity, unit_price, parent order_total + status. Dashboard reads this for the "yesterday" (sale_date = CURRENT_DATE - 1) callout. Same status filter as ops.orders_rollup.';

-- -----------------------------------------------------------------------------
-- GRANTs — ops.* convention: brian_admin owns these (creator/superuser); read
-- roles forge_ops + mcp_readonly get SELECT so agents/MCP get the safe derived
-- projection (they cannot see raw.* directly).
-- -----------------------------------------------------------------------------
GRANT SELECT ON ops.orders_rollup      TO forge_ops, mcp_readonly;
GRANT SELECT ON ops.orders_week_detail TO forge_ops, mcp_readonly;
GRANT SELECT ON ops.orders_recent      TO forge_ops, mcp_readonly;

COMMIT;

-- =============================================================================
-- ROLLBACK (manual; run only to fully reverse this migration)
-- -----------------------------------------------------------------------------
-- BEGIN;
-- DROP VIEW IF EXISTS ops.orders_recent;
-- DROP VIEW IF EXISTS ops.orders_week_detail;
-- DROP VIEW IF EXISTS ops.orders_rollup;
-- COMMIT;
-- =============================================================================
