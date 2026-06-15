# CPG Analytics Platform

End-to-end sales analytics platform for a mid-size CPG company: data ingestion from Excel workbooks, data quality validation, revenue forecasting (Prophet + seasonal decomposition), AI-powered insights (DeepSeek), a FastAPI backend, and a Streamlit dashboard.

## Quick start

```bash
cp .env.example .env          # fill in secrets
docker-compose up --build     # starts postgres + api + ui
```

API docs: http://localhost:8000/docs  
UI: http://localhost:8501

## Project layout

```
cpg-analytics/
  data/
    input/historical/       # bulk-load .xlsx workbooks
    input/incremental/      # ongoing batch drops
  data/output/
    processed/              # cleaned exports
    quality_reports/        # DQ logs per batch
    archive/                # inputs moved here after processing
  db/init/                  # SQL schema run on postgres init
  scripts/                  # synthetic data generator + utilities
  src/
    common/                 # config, db session, excel I/O, pydantic models
    ingestion/              # source mappings, loaders (historical + incremental)
    dq/                     # data-quality validators
    forecasting/            # Prophet wrappers + seasonal logic
    api/                    # FastAPI routers + schemas
  ui/                       # Streamlit app
  tests/
  docs/adr/                 # Architecture Decision Records
  .github/workflows/        # CI/CD
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
black --check src tests
```

## Data conventions

- All inputs are `.xlsx` workbooks (possibly multi-sheet, with leading metadata rows).
- Workbooks are placed in `data/input/historical/` (initial load) or `data/input/incremental/` (ongoing).
- After successful processing, inputs are moved to `data/output/archive/`.
- Cleaned data and DQ reports are written to `data/output/processed/` and `data/output/quality_reports/` respectively.
