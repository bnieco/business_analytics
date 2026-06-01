-- Ops queue: open demand per finished good, with on-hand count and net shortfall.
-- Wraps ops.demand_by_part with a sort so the tightest shortfalls surface first.
SELECT
    sku,
    name,
    open_units,
    open_orders,
    on_hand,
    net_shortfall,
    oldest_open_ordered_at,
    newest_open_ordered_at
FROM ops.demand_by_part
ORDER BY net_shortfall DESC NULLS LAST,
         oldest_open_ordered_at ASC;
