# CPG Analytics Platform — Architecture & Data Flow

## 1. What This System Does

End-to-end analytics platform for Consumer Packaged Goods (CPG) sales data.
It takes raw Excel workbooks from two sales channels (POS and Online), reads and cleans them,
and writes well-structured CSV files to a downstream folder that acts as a lightweight
table store for forecasting, reporting, and AI-powered insights.

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Data wrangling | pandas 2.x + openpyxl 3.x |
| Config format | JSON (stdlib `json` — no extra dependency) |
| Excel reading | openpyxl 3.x via `src/common/excel_io.py` |
| Settings / env | pydantic-settings 2.x |
| Logging | loguru |
| Database | PostgreSQL 16 (Docker) — schema present; used by forecasting phase |
| ORM | SQLAlchemy 2.x (available; not used by the ingestion pipeline) |
| Forecasting | Prophet (next phase) |
| API | FastAPI (next phase) |
| UI | Streamlit (next phase) |
| Containerisation | Docker + docker-compose |
| CI | GitHub Actions (next phase) |

---

## 3. Repository Layout

```
cpg-analytics/
├── config/
│   └── ingestion.json          ← single source of truth for all file/sheet routing
├── data/
│   ├── input/
│   │   ├── historical/         ← drop bulk Excel files here before first run
│   │   └── incremental/        ← drop daily/weekly batch files here
│   └── output/
│       └── downstream/         ← one CSV per entity type; written by the pipeline
├── db/
│   └── init/
│       └── 01_schema.sql       ← PostgreSQL DDL (applied by Docker on first start)
├── scripts/
│   └── generate_data.py        ← synthetic data generator (seed=42, deterministic)
├── src/
│   ├── common/
│   │   ├── config.py           ← Settings (env vars via pydantic-settings)
│   │   ├── db.py               ← SQLAlchemy engine + get_session() (forecasting phase)
│   │   └── excel_io.py         ← robust .xlsx reader: ghost-sheet skip, header detection
│   ├── dq/
│   │   └── __init__.py
│   ├── forecasting/
│   │   └── __init__.py         ← Prophet forecasting (next phase)
│   └── ingestion/
│       ├── config_loader.py    ← loads ingestion.json into typed dataclasses
│       └── pipeline.py         ← read → clean → write CSV orchestration
├── .env.example                ← all env vars with defaults
├── docker-compose.yml
└── pyproject.toml
```

---

## 4. The Data Universe

### Dimensions

| Entity | Count | Notes |
|---|---|---|
| Regions | 4 | NORTHEAST, SOUTHEAST, MIDWEST, WEST |
| POS stores | 8 | 2 per region; types SUPERMARKET / CONVENIENCE |
| Online locations | 4 | One virtual store per region (ONLINE-NE, ONLINE-SE, etc.) |
| SKUs | 12 | Across 5 categories |
| Categories | 5 | Beverages, Snacks, Personal Care, Household, Frozen Foods |

### Historical window
`2022-07-01` → `2024-06-30` (~24 months)

### Incremental batches
Three weekly batches: `2024-07-01`, `2024-07-08`, `2024-07-15`

---

## 5. Data Generation (`scripts/generate_data.py`)

Run once to populate `data/input/` before the ingestion pipeline:

```bash
python3 scripts/generate_data.py          # seed=42, root=.
python3 scripts/generate_data.py --seed 7 --root /custom/path
```

### Signal model

Each transaction's quantity is drawn from a Poisson distribution whose rate
is multiplied by compounding factors:

```
demand_rate = base_rate
            × trend_factor(month)       # +0.2% per month linear growth
            × weekly_factor(day)        # ×1.3 on Fri/Sat/Sun
            × yearly_factor(month)      # ×1.35 in Oct/Nov/Dec (Q4 lift)
            × promo_factor              # ×1.25 when (date, category, region) in promo calendar
            × lognormal_noise(σ=0.18)   # ±18% random variation per transaction
```

Revenue = `quantity × list_price` for POS; the `amount` column is absent in Online
(the pipeline handles missing revenue without failing).

### Intentional data quality issues (~5–8% of rows)

The generator injects realistic problems using non-overlapping index sets:

| Issue | Schema | Present in |
|---|---|---|
| NULL unit_price | A (POS) | `pos_sales_history.xlsx` |
| NULL amount / revenue | A (POS) | `pos_sales_history.xlsx` |
| EUR currency (needs conversion) | A & B | both sales files |
| Negative / zero quantity | A & B | both sales files |
| Unknown store_id (STORE999) | A | `pos_sales_history.xlsx` |
| Unknown SKU | B | `online_sales_history.xlsx` |

The pipeline's `clean_dataframe()` handles null values, date parsing, and numeric coercion.
Rows are kept as-is (not dropped) unless they are entirely empty.

### Output files written by the generator

| File | Location | Content |
|---|---|---|
| `historical_data.xlsx` | `data/input/historical/` | Multi-sheet: dim_region, dim_store, dim_product, seasonal_calendar |
| `pos_sales_history.xlsx` | `data/input/historical/` | Sheet "Sales" — Schema A |
| `online_sales_history.xlsx` | `data/input/historical/` | Sheet "Orders" — Schema B (no amount column) |
| `promo_windows.xlsx` | `data/input/historical/` | Sheet "PromoWindows" |
| `marketing_campaigns.xlsx` | `data/input/historical/` | Sheet "Campaigns" |
| `competitor_prices.xlsx` | `data/input/historical/` | Sheet "CompetitorPrices" |
| `2024-07-01_pos.xlsx` | `data/input/incremental/` | Sheet "Sales" with embedded duplicates |
| `2024-07-08_online.xlsx` | `data/input/incremental/` | Sheet "Orders" + sheet "product_updates" |
| `2024-07-15_pos.xlsx` | `data/input/incremental/` | Sheet "Sales" (title row above header) + ghost "Notes" sheet |

---

## 6. Ingestion Configuration (`config/ingestion.json`)

**The only place where file names, sheet names, column maps, and routing rules live.**
No file or sheet name is hardcoded in Python — everything is driven by this file.

### Top-level structure

```json
{
  "settings": {
    "header_scan_rows": 15,
    "null_values": ["", "NA", "N/A", "NULL", "None", "nan", "NaN", ...],
    "date_formats": ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", ...],
    "downstream_dir": "data/output/downstream"
  },
  "file_groups": [ ... ]
}
```

### File group structure

```json
{
  "name": "pos_history",
  "description": "POS historical sales",
  "dir": "data/input/historical",
  "file_pattern": "*pos*.xlsx",
  "enabled": true,
  "sheets": [
    {
      "sheet": "*",
      "target_csv": "sales_transactions.csv",
      "write_mode": "append",
      "enabled": true,
      "column_map": {
        "ts":     "transaction_ts",
        "qty":    "quantity",
        "amount": "revenue"
      },
      "add_columns": {
        "source_system": "POS",
        "source_file":   null
      },
      "dedup_column": "transaction_id"
    }
  ]
}
```

### Field reference

| Field | Where | Effect |
|---|---|---|
| `file_pattern` | file group | Glob — e.g. `"*.xlsx"`, `"*pos*.xlsx"`. All matching files in `dir` are processed. |
| `enabled: false` | file group or sheet | Skip entirely |
| `sheet: "*"` | sheet | Match every non-ghost sheet in the workbook |
| `write_mode: "overwrite"` | sheet | Truncates the target CSV before writing |
| `write_mode: "append"` | sheet | Appends rows; aligns to existing column schema automatically |
| `column_map` | sheet | When present: only listed source columns are kept and renamed. When absent: all columns pass through. |
| `add_columns` | sheet | Injects static columns. `null` value → use the source filename at runtime. |
| `dedup_column` | sheet | In-batch deduplication on this column before writing |

### The 7 file groups and their target CSVs

| Group | Pattern | Sheets matched | Target CSV | Mode |
|---|---|---|---|---|
| `reference_dimensions` | `historical_data.xlsx` | dim_region, dim_store, dim_product, seasonal_calendar | one CSV each | overwrite |
| `promo_windows` | `promo_windows.xlsx` | `*` | `promo_windows.csv` | overwrite |
| `marketing_campaigns` | `marketing_campaigns.xlsx` | `*` | `marketing_campaigns.csv` | overwrite |
| `competitor_prices` | `competitor_prices.xlsx` | `*` | `competitor_prices.csv` | overwrite |
| `pos_history` | `*pos*.xlsx` | `*` | `sales_transactions.csv` | **overwrite** (first write; clears CSV) |
| `online_history` | `*online*.xlsx` | `*` | `sales_transactions.csv` | **append** (adds ONLINE rows to POS rows) |
| `incremental_batches` | `*.xlsx` | Sales, Orders, product_updates | `sales_transactions.csv` / `product_updates.csv` | append |

---

## 7. Pipeline Flow (`src/ingestion/pipeline.py`)

**Command:**
```bash
python3 -m src.ingestion.pipeline [--root .] [--config config/ingestion.json]
```

Every file group goes through the same three-stage pipeline per sheet:

```
config/ingestion.json
        │
        ▼
src/ingestion/config_loader.py
  load_config()  →  IngestionConfig (dataclasses, no Pydantic)
        │
        ▼  for each enabled file group
        │
        ├─ resolve files: root / group.dir  glob(group.file_pattern)
        │
        ▼  for each file
        │
Stage 1 ── READ ─────────────────────────────────────────────────────────────
│  src/common/excel_io.read_workbook(path)
│    • Opens with openpyxl data_only=True  (formula cells → cached values)
│    • Ghost-sheet skip: sheets with 0 non-empty cells are silently ignored
│    • Header-row detection: scans first N rows (configurable), picks the row
│      with the most non-null cells — handles title rows above the header
│    • Returns {sheet_name: DataFrame} for all non-ghost sheets
│
Stage 2 ── CLEAN ────────────────────────────────────────────────────────────
│  pipeline.clean_dataframe(df, settings)
│    1. Column names → lowercase + strip whitespace
│    2. Null markers replaced with pd.NA  (list is configurable)
│    3. Fully-empty rows dropped
│    4. String values → strip whitespace
│    5. Date-like columns parsed  (heuristic: column name contains _date / _ts
│       / _time / datetime / timestamp; tries each configured date_format)
│    6. Numeric coercion of string values  (strips $, €, commas; coerces only
│       if > 50% of non-null values parse successfully — ID columns like
│       transaction_id, store_id are protected from coercion)
│
Stage 3 ── WRITE CSV ────────────────────────────────────────────────────────
   pipeline.apply_sheet_config(df, sheet_cfg, filename)
     • column_map  → keep & rename only mapped columns (drop the rest)
     • add_columns → inject source_system, source_file, etc.
     • dedup_column → drop in-batch duplicates
   pipeline.write_csv(df, dest, write_mode)
     • overwrite   → df.to_csv(dest)  (truncates)
     • append      → read existing headers, reindex df to match, then
                     df.to_csv(dest, mode="a", header=False)
                     Ensures POS rows and ONLINE rows (different schemas)
                     merge into one well-formed CSV
```

---

## 8. Excel Reading (`src/common/excel_io.py`)

Reused by the pipeline for all workbook opens.

### Ghost sheet detection
A sheet is "ghost" (silently skipped) when it has zero non-empty cells.
Handles stale placeholder tabs left behind by Excel users.

### Header row detection
Does NOT assume row 1 is the header. Scans up to `header_scan_rows` rows
and picks the row with the most non-null cells.

```
Row 1: "Monthly POS Report — July 2024"   → 1 non-null cell   (title, ignored)
Row 2: transaction_id | ts | store_id | … → 8 non-null cells  (header, chosen)
Row 3: TXN001 | 2024-07-01 | STORE001 | … → data
```

The `2024-07-15_pos.xlsx` incremental file exercises this path: the generator adds
a title row, and the pipeline reads it correctly without any special handling.

---

## 9. Downstream CSV Files (`data/output/downstream/`)

After a full pipeline run, 9 CSV files are produced:

| CSV | Rows | Description |
|---|---|---|
| `sales_transactions.csv` | ~40,956 | POS history + ONLINE history + 3 incremental batches, all merged |
| `dim_product.csv` | 12 | Product master (SKU, category, brand, price) |
| `dim_region.csv` | 4 | Region reference |
| `dim_store.csv` | 12 | Store reference (8 POS + 4 Online locations) |
| `seasonal_calendar.csv` | 1,280 | One row per calendar date with season + holiday flag |
| `promo_windows.csv` | 8 | Promotional periods by category + region |
| `marketing_campaigns.csv` | 6 | Campaign metadata |
| `competitor_prices.csv` | 480 | Weekly competitor observations |
| `product_updates.csv` | 2 | Product attribute change log from incremental batches |

### `sales_transactions.csv` schema

| Column | Source | Notes |
|---|---|---|
| `transaction_id` | All sources | POS uses `transaction_id`; ONLINE uses `order_id` (remapped) |
| `transaction_ts` | All sources | POS `ts`; ONLINE `order_datetime` (remapped); parsed to datetime |
| `store_id` | All sources | POS `store_id`; ONLINE `location_id` (remapped) |
| `sku` | All sources | POS `sku`; ONLINE `product_sku` (remapped) |
| `quantity` | All sources | POS `qty`; ONLINE `units` (remapped) |
| `unit_price` | All sources | POS `unit_price`; ONLINE `price_per_unit` (remapped) |
| `revenue` | POS only | `amount` remapped; absent in ONLINE rows (NaN) |
| `currency` | All sources | USD or EUR (EUR present in source data as injected DQ issue) |
| `source_system` | Added | `"POS"` or `"ONLINE"` (static injection from config) |
| `source_file` | Added | Filename of the workbook that provided the row |

### Append behaviour and re-runs

- The **first** file group that writes to a CSV uses `write_mode: "overwrite"` →
  truncates on every pipeline run (no stale data accumulates for historical feeds).
- Subsequent groups use `write_mode: "append"` → rows stack cleanly.
- Running the full pipeline twice gives the same result for `overwrite` CSVs;
  `append`-only CSVs (incremental) will accumulate. Delete the downstream folder
  or set incremental groups to `write_mode: "overwrite"` if a clean re-run is needed.

---

## 10. Config Loader (`src/ingestion/config_loader.py`)

Parses `ingestion.json` into plain Python dataclasses — no Pydantic, no YAML.

```
ingestion.json
    │
    └── load_config(path) → IngestionConfig
            ├── settings: PipelineSettings
            │       header_scan_rows, null_values, date_formats, downstream_dir
            └── file_groups: list[FileGroup]
                    ├── name, dir, file_pattern, enabled, description
                    └── sheets: list[SheetConfig]
                            sheet, target_csv, write_mode, enabled,
                            column_map, add_columns, dedup_column
```

Unknown keys in the JSON are silently ignored, so the config is forward-compatible.

---

## 11. How to Run

### Prerequisites
```bash
cd cpg-analytics
cp .env.example .env
pip install -e ".[dev]"
```

Postgres is only needed for the forecasting phase (not for the ingestion pipeline):
```bash
docker compose up postgres -d   # optional for ingestion-only work
```

### Step 1 — Generate synthetic data
```bash
python3 scripts/generate_data.py
```
Writes all historical and incremental `.xlsx` files to `data/input/`.

### Step 2 — Run the pipeline
```bash
python3 -m src.ingestion.pipeline
```
Reads every enabled file group from `config/ingestion.json` in order and writes
CSVs to `data/output/downstream/`.

### Step 3 — Use a custom config or different root
```bash
python3 -m src.ingestion.pipeline --config /path/to/other.json
python3 -m src.ingestion.pipeline --root /data/project
# or via env var:
INGESTION_CONFIG=/path/to/other.json python3 -m src.ingestion.pipeline
```

### Adding a new data source (no Python needed)
1. Drop the new `.xlsx` into `data/input/historical/` or `data/input/incremental/`
2. Add a file group block to `config/ingestion.json`:
```json
{
  "name": "new_feed",
  "dir": "data/input/historical",
  "file_pattern": "new_feed.xlsx",
  "enabled": true,
  "sheets": [
    {
      "sheet": "*",
      "target_csv": "new_feed.csv",
      "write_mode": "overwrite",
      "enabled": true
    }
  ]
}
```
3. Re-run `python3 -m src.ingestion.pipeline`

---

## 12. End-to-End Data Flow

```
scripts/generate_data.py
        │
        ├─ data/input/historical/*.xlsx
        └─ data/input/incremental/*.xlsx
                │
                ▼
        config/ingestion.json
          7 file groups; each declares:
            dir, file_pattern, sheet names, column_map, target_csv
                │
                ▼
        src/ingestion/config_loader.py
          load_config() → IngestionConfig (dataclasses)
                │
                ▼
        src/ingestion/pipeline.run_pipeline()
          for each file group:
            glob(dir, file_pattern) → sorted list of files
            for each file:
              │
              ├─ Stage 1: src/common/excel_io.read_workbook()
              │     openpyxl data_only=True
              │     ghost-sheet skip
              │     header-row auto-detection
              │     → {sheet_name: DataFrame}
              │
              ├─ Stage 2: pipeline.clean_dataframe()
              │     lowercase column names
              │     null marker replacement
              │     drop all-null rows
              │     whitespace strip
              │     date parsing (col name heuristic + configured formats)
              │     numeric coercion (currency symbols, commas; ID cols protected)
              │
              └─ Stage 3: pipeline.write_csv()
                    apply column_map   (rename + filter columns)
                    inject add_columns (source_system, source_file)
                    in-batch dedup     (optional dedup_column)
                    overwrite → truncate + write with header
                    append   → reindex to existing schema + append rows
                        │
                        ▼
              data/output/downstream/
                ├── sales_transactions.csv   (~40,956 rows — POS + ONLINE + incremental)
                ├── dim_product.csv
                ├── dim_region.csv
                ├── dim_store.csv
                ├── seasonal_calendar.csv
                ├── promo_windows.csv
                ├── marketing_campaigns.csv
                ├── competitor_prices.csv
                └── product_updates.csv
```
