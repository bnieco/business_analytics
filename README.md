# business_analytics

Analysis + ingestion tooling for the `business` PostgreSQL database on the Synology NAS. Mirrors the `health_analytics` project layout.

The DDL for the `business` DB lives in the Business folder:
`Business/Elders-Nine-Relics/Analytics/business_db/migrations/`

## Setup

```bash
cd ~/PycharmProjects/business_analytics
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit .env to add BUSINESS_DB_PASSWORD
chmod 600 .env
```

VS Code will pick up the venv automatically via `.vscode/settings.json`.

## Layout

```
business_analytics/
├── .env                     # connection details (gitignored)
├── .env.example             # template
├── requirements.txt
├── scripts/
│   ├── db.py                # shared connection helpers (query, query_file)
│   ├── run_query.py         # CLI: run a saved SQL file
│   ├── load_etsy.py         # Etsy CSVs → business.raw (idempotent per file)
│   └── enter_direct_order.py # Interactive CLI for Direct order entry
├── queries/
│   ├── 01_open_orders.sql
│   ├── 02_demand_by_part.sql
│   ├── 03_customers_summary.sql
│   └── 04_listings_without_part_mapping.sql
└── notebooks/
    └── 01_exploration.ipynb
```

## Usage

**Load Etsy CSVs into `business.raw`:**
```bash
./venv/bin/python scripts/load_etsy.py            # all CSVs from _Data/ENR/Etsy
./venv/bin/python scripts/load_etsy.py --dry-run  # parse + report, no inserts
./venv/bin/python scripts/load_etsy.py --file /path/to/EtsySoldOrders2026.csv
```

**Enter a Direct order:**
```bash
./venv/bin/python scripts/enter_direct_order.py
```

**Run a saved query:**
```bash
./venv/bin/python scripts/run_query.py queries/01_open_orders.sql
```

**Notebook — interactive exploration:**
Open `notebooks/01_exploration.ipynb` in VS Code, select the venv as kernel, run cells.

**Python — ad-hoc:**
```python
from db import query
df = query("SELECT * FROM core.parts ORDER BY sku")
```

## Schema reference

| Schema | Purpose |
|---|---|
| `raw`  | Etsy CSV landing tables + `listing_id_map` (many listing_ids → canonical part) |
| `core` | Canonical entities: customers, parts, bom_versions, bom_components, orders, order_items |
| `ops`  | Current-state `parts_on_hand`; `demand_by_part` view |

## Next steps (open work)

1. Run `load_etsy.py` against `_Data/ENR/Etsy/` to populate `business.raw`.
2. Seed `core.parts` from distinct listings; curate `raw.listing_id_map` so every order-item `listing_id` resolves to a `part_id`.
3. Write the staging transform: `raw.etsy_orders` + `raw.etsy_order_items` → `core.customers` / `core.orders` / `core.order_items` via the curated mapping.
4. Populate `ops.parts_on_hand` with current counts.
5. Expand `ops.demand_by_part` to v2 (BOM explosion for component demand).
