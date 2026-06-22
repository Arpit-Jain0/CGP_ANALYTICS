"""
Shared pytest fixtures for the CPG Analytics test suite.

Structure
---------
- wb_factory      — creates .xlsx workbooks in tmp_path (no I/O beyond the temp dir)
- downstream_csvs — writes minimal CSV files that the API CSV-backed queries expect
- api_client      — FastAPI TestClient with ALL DB and LLM calls mocked out
- db_engine       — SQLAlchemy engine against TEST_DATABASE_URL (skips when absent)
- db_session      — transactional session that rolls back after each DB test
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import openpyxl
import pandas as pd
import pytest
from fastapi.testclient import TestClient

# ── Environment ───────────────────────────────────────────────────────────────

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://cpg:changeme@localhost:5432/cpg_analytics_test",
)
requires_db = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="Set TEST_DATABASE_URL to run integration tests",
)


# ── Excel / workbook helpers ──────────────────────────────────────────────────


@pytest.fixture
def wb_factory(tmp_path):
    """
    Return a factory function that writes an .xlsx file to tmp_path.

    Usage
    -----
    path = wb_factory("test.xlsx", {"Sheet1": [["col_a", "col_b"], [1, 2], [3, 4]]})
    """

    def _make(name: str, sheets: dict[str, list[list]]) -> Path:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for sheet_name, rows in sheets.items():
            ws = wb.create_sheet(title=sheet_name)
            for row in rows:
                ws.append(row)
        path = tmp_path / name
        wb.save(str(path))
        return path

    return _make


# ── Minimal downstream CSVs ───────────────────────────────────────────────────


@pytest.fixture
def downstream_csvs(tmp_path):
    """
    Write the minimum CSV files that the API's revenue aggregation queries need,
    then return the directory path.  Tests that need them should monkeypatch
    src.api.queries.downstream_dir to return this path.
    """
    ds = tmp_path / "downstream"
    ds.mkdir()

    pd.DataFrame(
        {
            "transaction_id": ["T001", "T002", "T003", "T004"],
            "transaction_ts": [
                "2024-01-10 10:00:00",
                "2024-01-11 11:00:00",
                "2024-01-12 12:00:00",
                "2024-01-13 13:00:00",
            ],
            "store_id": ["S01", "S01", "S02", "S02"],
            "sku": ["SKU01", "SKU02", "SKU01", "SKU02"],
            "quantity": [2, 3, 1, 4],
            "unit_price": [10.0, 20.0, 10.0, 20.0],
            "revenue": [20.0, 60.0, 10.0, 80.0],
            "currency": ["USD"] * 4,
            "source_system": ["POS", "POS", "ONLINE", "ONLINE"],
            "source_file": ["test.xlsx"] * 4,
        }
    ).to_csv(ds / "sales_transactions.csv", index=False)

    pd.DataFrame({"sku": ["SKU01", "SKU02"], "category": ["Beverages", "Snacks"]}).to_csv(
        ds / "dim_product.csv", index=False
    )

    pd.DataFrame(
        {
            "store_id": ["S01", "S02"],
            "region": ["NORTHEAST", "SOUTHEAST"],
            "city": ["New York", "Atlanta"],
            "store_type": ["SUPERMARKET", "CONVENIENCE"],
        }
    ).to_csv(ds / "dim_store.csv", index=False)

    return ds


# ── Canned return values for API mocks ────────────────────────────────────────

_MOCK_KPIS = {
    "total_revenue": 170.0,
    "top_category": "Beverages",
    "top_region": "NORTHEAST",
    "transaction_count": 4,
    "by_category": [{"category": "Beverages", "revenue": 90.0}],
    "by_region": [{"region": "NORTHEAST", "revenue": 80.0}],
    "start_date": None,
    "end_date": None,
}
_MOCK_QUALITY = {
    "total_issues": 2,
    "by_issue_type": [{"issue_type": "NULL_REQUIRED", "count": 2}],
    "by_action_taken": [{"action_taken": "REPAIRED", "count": 2}],
    "total_batches": 1,
    "latest_batch": {
        "load_batch_id": 1,
        "load_type": "HISTORICAL",
        "source_file": "test.xlsx",
        "rows_in": 10,
        "inserted": 8,
        "deduped": 2,
        "rejected": 0,
        "repaired": 2,
        "flagged": 0,
        "late_arriving": 0,
    },
}
_MOCK_FORECAST = {
    "run_date": date(2024, 3, 1),
    "model_version": "prophet-v1",
    "points": [
        {
            "target_date": date(2024, 3, 2),
            "predicted_revenue": 55.5,
            "yhat_lower": 40.0,
            "yhat_upper": 70.0,
        }
    ],
}
_MOCK_AGG = {
    "total_revenue": 170.0,
    "by_category": [{"category": "Beverages", "revenue": 90.0}],
    "by_region": [{"region": "NORTHEAST", "revenue": 80.0}],
}


# ── API test client ────────────────────────────────────────────────────────────


@pytest.fixture
def api_client():
    """
    A FastAPI TestClient with every external dependency mocked so tests
    never hit a real database or a real LLM.

    Patches applied
    ---------------
    - ping()                   → True  (DB reachable)
    - get_revenue_kpis         → _MOCK_KPIS
    - get_quality_summary      → _MOCK_QUALITY
    - get_forecast_rows        → _MOCK_FORECAST
    - run_ingest               → synthetic stats dict
    - get_insights_aggregates  → _MOCK_AGG
    - build_bounded_context    → "context text"
    - generate_insights        → AsyncMock returning ("Insight text", False)
    - answer_question          → AsyncMock returning ("Answer text", False)
    """
    from src.api.main import app

    patches = [
        patch("src.api.main.ping", return_value=True),
        patch("src.api.routes.health.ping", return_value=True),
        patch("src.api.routes.summary.get_revenue_kpis", return_value=_MOCK_KPIS),
        patch("src.api.routes.quality.get_quality_summary", return_value=_MOCK_QUALITY),
        patch("src.api.routes.forecast.get_forecast_rows", return_value=_MOCK_FORECAST),
        patch(
            "src.api.routes.ingest.run_ingest",
            return_value={"files_processed": 1, "inserted": 5, "load_batch_id": 1},
        ),
        patch("src.api.routes.insights.get_insights_aggregates", return_value=_MOCK_AGG),
        patch("src.api.routes.ask.build_bounded_context", return_value="context text"),
        patch(
            "src.api.routes.insights.generate_insights",
            new=AsyncMock(return_value=("Insight text", False)),
        ),
        patch(
            "src.api.routes.ask.answer_question",
            new=AsyncMock(return_value=("Answer text", False)),
        ),
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with TestClient(app) as client:
            yield client


# ── DB fixtures (integration tests only) ─────────────────────────────────────


@pytest.fixture(scope="session")
def db_engine():
    """
    SQLAlchemy engine connected to TEST_DATABASE_URL.
    Skipped when TEST_DATABASE_URL is not set.
    Creates the full schema on first use.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)

    schema_path = Path(__file__).resolve().parents[1] / "db" / "init" / "01_schema.sql"
    ddl = schema_path.read_text(encoding="utf-8")

    def _has_sql(fragment: str) -> bool:
        return any(
            line.strip() and not line.strip().startswith("--") for line in fragment.splitlines()
        )

    with engine.connect() as conn:
        for stmt in ddl.split(";"):
            stmt = stmt.strip()
            if stmt and _has_sql(stmt):
                conn.execute(text(stmt))
        conn.commit()

    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """
    Yields a SQLAlchemy session; rolls back and cleans test rows after each test.
    Tables cleaned: forecast_results, data_quality_log, load_batch, dim_product,
    sales_transactions.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.rollback()
        for table in [
            "forecast_results",
            "data_quality_log",
            "sales_transactions",
            "load_batch",
            "dim_product",
            "dim_region",
            "dim_store",
        ]:
            session.execute(text(f"DELETE FROM {table}"))
        session.commit()
        session.close()
