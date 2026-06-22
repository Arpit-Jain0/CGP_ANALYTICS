# CPG Analytics Platform

End-to-end analytics platform: Excel → 7-stage ingestion pipeline → Prophet revenue forecasts → FastAPI → Streamlit dashboard with LLM Q&A.

---

## Stack

| Layer | Tech |
|---|---|
| Language | Python 3.11 |
| Ingestion | pandas 2.x, openpyxl 3.x |
| Database | PostgreSQL 16 |
| ORM | SQLAlchemy 2.x |
| Forecasting | Facebook Prophet 1.x |
| API | FastAPI 0.137+ |
| UI | Streamlit 1.35+, Altair 5.x |
| LLM | Ollama (local Docker container; fallback built-in) |
| Containers | Docker + Docker Compose |
| CI | GitHub Actions |

---

## Directory Layout

```
CPG_analytics/
├── config/
│   └── ingestion.json          ← file/sheet routing; no Python hardcoding
├── data/
│   ├── input/
│   │   ├── historical/         ← bulk Excel files (run generate_data.py once)
│   │   └── incremental/        ← weekly batch files
│   └── output/
│       ├── downstream/         ← 9 CSVs written by pipeline; read by API + forecaster
│       └── quality_reports/    ← per-sheet DQ violation reports
├── db/
│   └── init/01_schema.sql      ← Postgres DDL; auto-applied by Docker on first start
├── scripts/
│   └── generate_data.py        ← synthetic data generator (seed=42, deterministic)
├── src/
│   ├── common/
│   │   ├── config.py           ← Settings via pydantic-settings (reads .env)
│   │   ├── db.py               ← SQLAlchemy engine + get_session()
│   │   └── excel_io.py         ← xlsx reader: ghost-sheet skip, header auto-detect
│   ├── dq/
│   │   └── checker.py          ← 3 DQ checks per sheet before write
│   ├── ingestion/
│   │   ├── config_loader.py    ← parses ingestion.json → typed dataclasses
│   │   ├── pipeline.py         ← 7-stage pipeline orchestrator
│   │   └── db_writer.py        ← archive, write_raw, write_error, write_curated
│   ├── forecasting/
│   │   └── forecaster.py       ← Prophet per (category × region); writes forecast_results
│   └── api/
│       ├── main.py             ← FastAPI app + lifespan
│       ├── models.py           ← Pydantic response models
│       ├── queries.py          ← CSV reads + DB queries; LLM context builder
│       ├── llm.py              ← Ollama client + deterministic fallback
│       └── routes/             ← one file per endpoint group (13 endpoints total)
├── ui/
│   ├── app.py                  ← Streamlit entry point + st.navigation
│   ├── api_client.py           ← requests wrapper, one function per endpoint
│   └── pages/                  ← 9 pages (see UI section below)
├── tests/                      ← 64 tests across 5 files
├── .github/workflows/ci.yml    ← lint + test + Docker build on every push
├── Dockerfile.api
├── Dockerfile.ui
├── docker-compose.yml          ← 5 services: postgres, ollama, ollama-init, api, ui
├── pyproject.toml
└── .env.example
```

---

## Data Flow

```
scripts/generate_data.py
        │
        ▼
data/input/historical/*.xlsx    data/input/incremental/*.xlsx
        │                               │
        └───────────┬───────────────────┘
                    │
          ┌─────────▼──────────┐
          │  1. READ           │  excel_io.read_workbook()
          ├─────────▼──────────┤
          │  2. ARCHIVE        │  copy xlsx → data/archive/{date}/
          ├─────────▼──────────┤
          │  3. RAW            │  insert all rows as TEXT → raw.* (Postgres)
          ├─────────▼──────────┤
          │  4. CLEAN          │  null markers, date/numeric parsing
          ├─────────▼──────────┤
          │  5. DQ CHECK       │  3 checks; rejected → error.dq_rejected_rows + CSV
          ├─────────▼──────────┤
          │  6. MAP            │  column rename + static column injection
          ├─────────▼──────────┤
          │  7. WRITE          │  curated.* (Postgres) + data/output/downstream/*.csv
          └────────────────────┘
                    │
       data/output/downstream/  ← 9 CSVs (sales_transactions, dim_product,
                    │              dim_store, dim_region, seasonal_calendar,
                    │              promo_windows, marketing_campaigns,
                    │              competitor_prices, product_updates)
                    │
        ┌───────────▼───────────┐
        │  forecaster.py        │  joins CSVs → daily (category, region) series
        │  Prophet × 20 models  │  fits + predicts → writes forecast_results (DB)
        └───────────▼───────────┘
                    │
        ┌───────────▼───────────┐
        │  FastAPI (13 endpoints)│  reads downstream CSVs + Postgres
        └───────────▼───────────┘
                    │
        ┌───────────▼───────────┐
        │  Streamlit (9 pages)  │  talks only to FastAPI — never to DB directly
        └───────────────────────┘
```

**Postgres layers (database: `cpg_analytics`):**

| Schema | Tables | Written by |
|---|---|---|
| `raw.*` | 10 tables — source TEXT, full row history | pipeline stage 3 |
| `error.*` | `dq_rejected_rows` (JSONB row + issue detail) | pipeline stage 5 |
| `curated.*` | 9 typed + indexed tables | pipeline stage 7 |
| `public` | `load_batch`, `data_quality_log`, `forecast_results` | ingest API + forecaster |

---

## Quick Start

### Option A — Docker (recommended)

Ollama runs as a **Docker container**, not a host install. On first run it downloads the LLM model (~4.7 GB) into a named volume. Subsequent starts use the cached model.

```bash
git clone <repo-url>
cd <repo-folder>
cp .env.example .env

docker compose up --build
# First run: 5–15 min (Ollama model download). API uses deterministic fallback while downloading.
```

Once healthy, run the one-time data setup:

```bash
# 1. Generate synthetic Excel input files (writes to ./data/input/ on the host)
python3 scripts/generate_data.py

# 2. Ingest historical data (all reference dims + 2 years of transactions)
docker compose exec api python3 -m src.ingestion.pipeline --mode historical

# 3. Ingest the 3 pre-seeded incremental batches
docker compose exec api python3 -m src.ingestion.pipeline --mode incremental

# 4. Train Prophet models and write 90-day forecasts to DB
docker compose exec api python3 -m src.forecasting.forecaster
```

> `generate_data.py` writes files to `./data/input/` on the host. The `./data` folder is bind-mounted into the api container, so the pipeline sees the same files when run via `docker compose exec`.

| Service | URL |
|---|---|
| Streamlit UI | http://localhost:8501 |
| FastAPI + Swagger | http://localhost:8000/docs |
| Postgres | localhost:5432 |
| Ollama | http://localhost:11434 |

---

### Option B — Local dev (Python + Docker for Postgres only)

Prerequisites: Python 3.11+, Docker Desktop

```bash
git clone <repo-url>
cd <repo-folder>
cp .env.example .env
pip install -e ".[dev]"

# Postgres only in Docker
docker compose up postgres -d

# Generate data
python3 scripts/generate_data.py

# Ingest
python3 -m src.ingestion.pipeline --mode historical
python3 -m src.ingestion.pipeline --mode incremental

# Forecast
python3 -m src.forecasting.forecaster

# API — keep running in a separate terminal
uvicorn src.api.main:app --reload --port 8000

# UI — keep running in a separate terminal
streamlit run ui/app.py
```

---

## Data Generation

`scripts/generate_data.py` creates all Excel input files. Run once before ingestion.

```bash
python3 scripts/generate_data.py          # default seed=42
python3 scripts/generate_data.py --seed 7  # different seed
```

Files created:

| File | Location | Contents |
|---|---|---|
| `historical_data.xlsx` | `data/input/historical/` | 4 sheets: dim_product, dim_region, dim_store, seasonal_calendar |
| `pos_sales_history.xlsx` | `data/input/historical/` | ~30K POS transactions, 2 years, Schema A |
| `online_sales_history.xlsx` | `data/input/historical/` | ~13K online orders, 2 years, Schema B (no amount column) |
| `promo_windows.xlsx` | `data/input/historical/` | 8 promotion periods |
| `marketing_campaigns.xlsx` | `data/input/historical/` | 6 marketing campaigns |
| `competitor_prices.xlsx` | `data/input/historical/` | Monthly competitor prices |
| `2024-07-01_pos.xlsx` | `data/input/incremental/` | Week 1 POS batch (with title row + ghost sheet + duplicate IDs) |
| `2024-07-08_online.xlsx` | `data/input/incremental/` | Week 2 online batch (with SCD2 change sheet + duplicate IDs) |
| `2024-07-15_pos.xlsx` | `data/input/incremental/` | Week 3 POS batch |

**Schema A** (POS): `transaction_id / ts / store_id / sku / qty / unit_price / amount / currency`
**Schema B** (Online): `order_id / order_datetime / location_id / product_sku / units / price_per_unit / currency` — no amount column; revenue derived as `unit_price × quantity` at pipeline time.

Each file has ~8.8% intentional DQ issues (null unit prices, mixed date formats, zero/negative quantities, unknown store/SKU IDs) to exercise the DQ checker.

**Live weekly batches** (for ongoing use):

```bash
# Generate this week's POS batch (idempotent — skips if already created this week)
curl -X POST "http://localhost:8000/generate-batch?type=pos"

# Generate this week's online batch
curl -X POST "http://localhost:8000/generate-batch?type=online"

# Then ingest
curl -X POST "http://localhost:8000/ingest?mode=incremental"
```

Or use the **🔄 Data Loads** page in the UI.

---

## Ingestion Pipeline

`config/ingestion.json` controls everything. No Python changes to add a new source.

**7 stages per sheet:**

| Stage | What happens |
|---|---|
| READ | Opens xlsx; skips ghost sheets; auto-detects header row (sparse first row = title row) |
| ARCHIVE | Copies original xlsx to `data/archive/{YYYY-MM-DD}/` |
| RAW | Inserts every row as TEXT into `raw.*` — no cleaning, no filtering |
| CLEAN | Lowercase cols; replace null markers; parse dates; coerce numerics; strip whitespace |
| DQ | 3 checks; rejected rows → `error.dq_rejected_rows` + `data/output/quality_reports/` CSV |
| MAP | Column rename + static field injection (e.g. `source_system=POS`) |
| WRITE | Typed rows → `curated.*` (Postgres) + `data/output/downstream/*.csv` |

**DQ checks:**

| Check | Issue type | Action |
|---|---|---|
| Identical rows in same batch | `DUPLICATE_ROW` | Keep first, drop rest |
| Same PK, different data | `PK_DUPLICATE` | Keep first, drop rest |
| Numeric col holds text, or qty ≤ 0 | `DATATYPE_VIOLATION` | Drop row |

**Commands:**

```bash
python3 -m src.ingestion.pipeline                    # all groups
python3 -m src.ingestion.pipeline --mode historical  # reference dims + history
python3 -m src.ingestion.pipeline --mode incremental # incremental group only

curl -X POST "http://localhost:8000/ingest?mode=historical"
curl -X POST "http://localhost:8000/ingest?mode=incremental"
```

**File groups and their outputs:**

| Group | Source file | Output CSV | Write mode |
|---|---|---|---|
| reference_dimensions | `historical_data.xlsx` | 4 dim CSVs | overwrite |
| promo_windows | `promo_windows.xlsx` | `promo_windows.csv` | overwrite |
| marketing_campaigns | `marketing_campaigns.xlsx` | `marketing_campaigns.csv` | overwrite |
| competitor_prices | `competitor_prices.xlsx` | `competitor_prices.csv` | overwrite |
| pos_history | `*pos*.xlsx` | `sales_transactions.csv` | overwrite |
| online_history | `*online*.xlsx` | `sales_transactions.csv` | append |
| incremental_batches | `data/input/incremental/*.xlsx` | `sales_transactions.csv` | append |

---

## Forecasting

One Prophet model per `(category × region)` pair = **20 models**. Reads downstream CSVs, writes to `forecast_results` table.

**Why Prophet:**

| Requirement | Why it fits |
|---|---|
| Weekly + yearly seasonality | Decomposes both as separate components; CPG weekends and Q4 are strong signals |
| Future external regressors | Accepts holiday and promo calendars for the full forecast horizon; ARIMA/ETS cannot |
| Multiplicative demand | Promo lift scales with trend level, not as a flat offset |
| Confidence intervals | Built-in 90% CI for inventory safety-stock planning |
| Missing days | Handles gaps without imputation |

Alternatives rejected: ARIMA (no future regressors), LSTM (needs far more data, harder to debug), ETS (no regressor support).

**Key config:**

```python
Prophet(
    seasonality_mode        = "multiplicative",
    interval_width          = 0.90,           # 90% CI
    changepoint_prior_scale = 0.05,           # moderate trend flexibility
)
m.add_regressor("promo_active", mode="multiplicative")  # discount_pct / 100
# Holiday upper_window=1 captures post-holiday spend
```

Pairs with fewer than 60 unique training dates are skipped.

**Commands:**

```bash
python3 -m src.forecasting.forecaster                  # 90-day forecast, all 20 pairs
python3 -m src.forecasting.forecaster --horizon 180    # extended horizon
python3 -m src.forecasting.forecaster --min-history 90 # stricter history guard

# Verify
docker exec -it <postgres-container> psql -U cpg -d cpg_analytics \
  -c "SELECT category, region, count(*) FROM forecast_results GROUP BY 1,2 ORDER BY 1,2;"
```

Re-run after every incremental ingest to incorporate the latest transactions.

**Output table `forecast_results`:**

| Column | Type | Notes |
|---|---|---|
| `run_date` | DATE | Forecast vintage |
| `category` | TEXT | Product category |
| `region` | TEXT | Sales region |
| `target_date` | DATE | Predicted date |
| `predicted_revenue` | NUMERIC(14,4) | Prophet `yhat` |
| `yhat_lower` | NUMERIC(14,4) | Lower 90% CI |
| `yhat_upper` | NUMERIC(14,4) | Upper 90% CI |
| `model_version` | TEXT | `"prophet-v1"` |

`UNIQUE (run_date, category, region, target_date)` + `ON CONFLICT DO UPDATE` — re-runs are idempotent.

---

## API Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{status, db_connected, version}` |
| POST | `/ingest?mode=historical\|incremental` | Runs pipeline; returns load_batch audit |
| POST | `/generate-batch?type=pos\|online` | Generates this week's incremental xlsx (idempotent) |
| GET | `/summary` | Revenue KPIs, top category/region, breakdowns |
| GET | `/quality` | DQ issue counts by type + action, latest load_batch |
| GET | `/forecast` | Prophet rows filtered by category, region, horizon |
| GET | `/products` | Top SKUs by revenue with brand + category |
| GET | `/dq-reports` | List of DQ report CSV files with per-check counts |
| GET | `/dq-reports/{filename}` | Rejected rows from one report |
| GET | `/db/overview` | Schema overview: all tables with row counts and column list |
| GET | `/db/table` | Browse rows of a specific Postgres table (used by DB Explorer page) |
| POST | `/insights` | Revenue aggregates → LLM narrative (fallback if Ollama down) |
| POST | `/ask` | Free-text Q&A — bounded context only; raw rows never sent to LLM |

```bash
# Sample calls
curl http://localhost:8000/health
curl http://localhost:8000/summary
curl "http://localhost:8000/forecast?category=Beverages&region=NORTHEAST&horizon=30"
curl "http://localhost:8000/products?limit=10"
curl http://localhost:8000/dq-reports
curl http://localhost:8000/db/overview
curl "http://localhost:8000/db/table?table=forecast_results&limit=10"
curl -X POST http://localhost:8000/insights
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which category has the highest revenue?"}'
```

Swagger UI: `http://localhost:8000/docs`

---

## UI Pages

All pages talk to the API only; no direct DB access.

| Page | What it shows |
|---|---|
| 🏠 Home | Platform status, quick stats, navigation cards, recent DQ violations |
| 📊 Dashboard | KPI tiles, revenue charts by category/region, product performance, DQ panel |
| 📈 Forecast | Category + region dropdowns, horizon slider, Prophet confidence-band chart |
| 🤖 AI Insights & Q&A | LLM narrative + free-text Q&A, LLM/fallback badge |
| 🛡 DQ Reports | Report index, chart by issue type, row-level drill-down, CSV download |
| 🔄 Data Loads | Historical + incremental load buttons, weekly batch generator, audit table |
| 🗄️ Database Explorer | Browse Postgres tables and schema |
| 🏗️ Architecture & Flow | System diagrams: combined, ingestion, forecasting, API+UI, Docker |
| 🧪 Test Coverage | What's tested, what's not, how to run |

```bash
streamlit run ui/app.py
# Opens at http://localhost:8501
```

---

## Tests

```bash
# Unit + API contract tests — no DB, no infra (~1.5 s)
pytest tests/ -q

# Include integration tests (needs Postgres)
TEST_DATABASE_URL=postgresql+psycopg2://cpg:changeme@localhost:5432/cpg_analytics_test \
  pytest tests/ -q

# With coverage
pytest tests/ --cov=src --cov-report=term-missing
```

| File | Tests | DB required |
|---|---|---|
| `test_excel_io.py` | 12 | No |
| `test_pipeline.py` | 9 | No |
| `test_api.py` | 28 | No (all deps mocked) |
| `test_forecaster.py` | 9 | No |
| `test_integration.py` | 6 | Yes (skipped without `TEST_DATABASE_URL`) |

CI runs all 64 tests on every push with a Postgres service container.

---

## Docker Services

```
postgres → ollama → api → ui
              ↑
         ollama-init (one-shot model pull, exits after)
```

| Service | Image | Port | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | 5432 | `db/init/01_schema.sql` auto-applied on first start |
| `ollama` | `ollama/ollama` | 11434 | LLM server; model cached in `ollama_data` volume |
| `ollama-init` | `ollama/ollama` | — | Pulls `llama3.1` (~4.7 GB) once, then exits; `restart: "no"` |
| `api` | `Dockerfile.api` | 8000 | Bind-mounts `./data` + `./config`; gets DB + Ollama URLs injected |
| `ui` | `Dockerfile.ui` | 8501 | Gets `API_BASE_URL=http://api:8000` injected |

**Ollama note:** Ollama is not installed on the host. It runs entirely inside Docker. The model weights persist in the `ollama_data` named volume so they survive container restarts. First download takes 5–15 minutes depending on connection speed.

---

## Environment Variables

Copy `.env.example` to `.env` — defaults work out of the box for local dev and Docker.

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | Set to `postgres` automatically in Docker |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_DB` | `cpg_analytics` | |
| `POSTGRES_USER` | `cpg` | |
| `POSTGRES_PASSWORD` | `changeme` | |
| `DATABASE_URL` | _(derived)_ | Override full DSN; docker-compose sets this automatically |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Set to `http://ollama:11434/v1` in Docker |
| `OLLAMA_MODEL` | `llama3.1` | Any pulled model, e.g. `mistral:7b`, `phi3:mini` |
| `API_BASE_URL` | `http://localhost:8000` | Set to `http://api:8000` in Docker |
| `INGESTION_CONFIG` | `config/ingestion.json` | Path to ingestion config |
| `LOG_LEVEL` | `INFO` | loguru level |

---

## Key Design Decisions

**CSVs as the query layer** — the pipeline writes 9 CSVs to `data/output/downstream/`. The API and forecaster read from these, not directly from Postgres. Makes data inspectable with any tool; removes DB dependency from the API layer.

**Config-driven ingestion** — `config/ingestion.json` owns all file names, sheet names, and column maps. Adding a new source = edit JSON only, no Python changes.

**Precomputed forecasts** — `forecaster.py` is a CLI batch job, not called on API request. The API serves from the `forecast_results` table. Keeps the API fast and stateless.

**Bounded LLM context** — `/ask` sends a pre-aggregated text context (<1,000 tokens) to Ollama, not raw rows. The LLM cannot hallucinate data it hasn't seen.

**LLM fallback** — both `/insights` and `/ask` return deterministic, useful answers when Ollama is not running. The whole stack works without any LLM.

**Extending the platform:**

| Task | Where to change |
|---|---|
| New Excel source | Add entry to `config/ingestion.json` |
| New DQ rule | `src/dq/checker.py` |
| New API endpoint | `src/api/routes/<name>.py` + register in `src/api/main.py` |
| New Prophet regressor | `src/forecasting/forecaster.py` — follow `build_promo_feature()` |
| New UI page | `ui/pages/<name>.py` + `st.Page(...)` in `ui/app.py` |
| Swap LLM | Change `OLLAMA_BASE_URL` + `OLLAMA_MODEL` in `.env` |
