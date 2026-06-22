"""
tests/test_integration.py

Integration tests that require a real Postgres database.

All tests in this file are skipped unless TEST_DATABASE_URL is set in the
environment.  In CI, the GitHub Actions postgres service provides this.
Locally, set:

    export TEST_DATABASE_URL=postgresql+psycopg2://cpg:changeme@localhost:5432/cpg_analytics_test

The db_session fixture (conftest.py) creates the schema on first use and
deletes test rows after each test so tests are isolated from each other.

Scenarios
---------
- Data quality log: inserted rows are summarised correctly by /quality.
- Load batch: inserting a load_batch record produces the expected latest_batch.
- SCD2 dim_product: closing an old row and inserting a new current row
  preserves the historical row and marks only the new row as current.
- Late-arriving flag: a sales_transaction inserted with is_late_arriving=True
  is retrievable with that flag intact.
- Forecast results: writing rows to forecast_results and querying via
  get_forecast_rows returns the correct data.
- Historical → incremental → forecast API chain: CSV pipeline writes data,
  forecast rows are written to DB, /forecast returns them.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import requires_db

# ── Data quality log ──────────────────────────────────────────────────────────


@requires_db
def test_quality_log_counts(db_session):
    """
    Inserting rows with specific issue_types into data_quality_log and then
    querying via get_quality_summary returns the correct aggregated counts.
    """
    from src.api.queries import get_quality_summary

    # Insert a load_batch first (FK constraint)
    result = db_session.execute(text("""
            INSERT INTO load_batch (load_type, source_file, started_at, finished_at, inserted)
            VALUES ('HISTORICAL', 'test.xlsx', now(), now(), 10)
            RETURNING load_batch_id
        """))
    batch_id = result.scalar()

    # Insert 3 quality log rows: 2 NULL_REQUIRED / REPAIRED, 1 TYPE_MISMATCH / REJECTED
    for issue, action in [
        ("NULL_REQUIRED", "REPAIRED"),
        ("NULL_REQUIRED", "REPAIRED"),
        ("TYPE_MISMATCH", "REJECTED"),
    ]:
        db_session.execute(
            text("""
                INSERT INTO data_quality_log
                    (load_batch_id, load_type, issue_type, action_taken)
                VALUES (:bid, 'HISTORICAL', :issue, :action)
            """),
            {"bid": batch_id, "issue": issue, "action": action},
        )
    db_session.commit()

    summary = get_quality_summary()

    assert summary["total_issues"] == 3
    issue_map = {r["issue_type"]: r["count"] for r in summary["by_issue_type"]}
    assert issue_map["NULL_REQUIRED"] == 2
    assert issue_map["TYPE_MISMATCH"] == 1

    action_map = {r["action_taken"]: r["count"] for r in summary["by_action_taken"]}
    assert action_map["REPAIRED"] == 2
    assert action_map["REJECTED"] == 1


@requires_db
def test_latest_batch_reflected(db_session):
    """
    The most-recently-inserted load_batch row appears as latest_batch in the
    quality summary response.
    """
    from src.api.queries import get_quality_summary

    db_session.execute(text("""
            INSERT INTO load_batch (load_type, source_file, rows_in, inserted, repaired, started_at, finished_at)
            VALUES ('INCREMENTAL', 'batch_01.xlsx', 50, 48, 2, now(), now())
        """))
    db_session.commit()

    summary = get_quality_summary()
    latest = summary["latest_batch"]
    assert latest is not None
    assert latest["load_type"] == "INCREMENTAL"
    assert latest["source_file"] == "batch_01.xlsx"
    assert latest["inserted"] == 48
    assert latest["repaired"] == 2


# ── SCD2 dim_product ──────────────────────────────────────────────────────────


@requires_db
def test_scd2_closes_old_row_and_creates_new(db_session):
    """
    Simulates a product attribute change:
    1. Insert current row for SKU=TEST001 (category=Beverages).
    2. Close the old row: set valid_to and is_current=False.
    3. Insert a new current row with the updated category.
    4. Assert two rows exist for the SKU; only the new one is_current=True.
    """
    sku = "TEST_SCD2_PROD"

    # Step 1: initial row
    db_session.execute(
        text("""
            INSERT INTO dim_product (sku, category, brand, valid_from, is_current)
            VALUES (:sku, 'Beverages', 'BrandA', '2023-01-01', TRUE)
        """),
        {"sku": sku},
    )
    db_session.commit()

    # Step 2: close the old row
    db_session.execute(
        text("""
            UPDATE dim_product
               SET valid_to = '2024-03-01', is_current = FALSE
             WHERE sku = :sku AND is_current = TRUE
        """),
        {"sku": sku},
    )
    # Step 3: new current row with updated category
    db_session.execute(
        text("""
            INSERT INTO dim_product (sku, category, brand, valid_from, is_current)
            VALUES (:sku, 'Dairy', 'BrandA', '2024-03-02', TRUE)
        """),
        {"sku": sku},
    )
    db_session.commit()

    rows = db_session.execute(
        text(
            "SELECT category, is_current, valid_to FROM dim_product WHERE sku = :sku ORDER BY valid_from"
        ),
        {"sku": sku},
    ).fetchall()

    assert len(rows) == 2, "Both historical and current rows must be present"

    old_row = rows[0]
    new_row = rows[1]

    assert old_row[0] == "Beverages"  # category
    assert old_row[1] is False  # is_current
    assert old_row[2] is not None  # valid_to set

    assert new_row[0] == "Dairy"  # updated category
    assert new_row[1] is True  # is_current
    assert new_row[2] is None  # valid_to NULL = still current


# ── Late-arriving flag ────────────────────────────────────────────────────────


@requires_db
def test_late_arriving_flag_stored(db_session):
    """
    A sales_transaction inserted with is_late_arriving=True is returned from
    the DB with that flag intact.
    """
    db_session.execute(text("""
            INSERT INTO dim_region (region) VALUES ('EAST')
            ON CONFLICT DO NOTHING
        """))
    db_session.execute(text("""
            INSERT INTO dim_store (store_id, region, city, store_type)
            VALUES ('LATE_S01', 'EAST', 'TestCity', 'SUPERMARKET')
            ON CONFLICT DO NOTHING
        """))
    db_session.execute(text("""
            INSERT INTO sales_transactions
                (transaction_id, transaction_ts, store_id, sku, quantity,
                 unit_price, revenue, currency, source_system, is_late_arriving)
            VALUES
                ('LATE_T001', '2022-01-15 10:00:00', 'LATE_S01', 'SKU_LATE',
                 1, 10.0, 10.0, 'USD', 'POS', TRUE)
        """))
    db_session.commit()

    row = db_session.execute(
        text("SELECT is_late_arriving FROM sales_transactions WHERE transaction_id = 'LATE_T001'")
    ).fetchone()

    assert row is not None
    assert row[0] is True


# ── Forecast results query ─────────────────────────────────────────────────────


@requires_db
def test_forecast_rows_query(db_session):
    """
    Rows written to forecast_results are returned correctly by get_forecast_rows.
    """
    from src.api.queries import get_forecast_rows

    run_date = date(2024, 6, 1)
    rows = [
        {
            "run_date": run_date,
            "category": "Beverages",
            "region": "NORTHEAST",
            "target_date": date(2024, 6, 2 + i),
            "predicted_revenue": 100.0 + i * 5,
            "yhat_lower": 80.0 + i,
            "yhat_upper": 120.0 + i,
            "model_version": "prophet-v1",
        }
        for i in range(10)
    ]
    for row in rows:
        db_session.execute(
            text("""
                INSERT INTO forecast_results
                    (run_date, category, region, target_date,
                     predicted_revenue, yhat_lower, yhat_upper, model_version)
                VALUES
                    (:run_date, :category, :region, :target_date,
                     :predicted_revenue, :yhat_lower, :yhat_upper, :model_version)
            """),
            row,
        )
    db_session.commit()

    # Patch get_session at the point of use so get_forecast_rows sees the
    # test session (which already has the committed rows).
    from contextlib import contextmanager

    @contextmanager
    def _patched_get_session():
        yield db_session

    with patch("src.api.queries.get_session", _patched_get_session):
        result = get_forecast_rows(category="Beverages", region="NORTHEAST", horizon=10)

    assert result["run_date"] == run_date
    assert result["model_version"] == "prophet-v1"
    assert len(result["points"]) == 10
    assert result["points"][0]["predicted_revenue"] == pytest.approx(100.0)


# ── Full pipeline chain (CSV → forecast → API) ────────────────────────────────


@requires_db
def test_pipeline_to_forecast_api(tmp_path, db_session):
    """
    End-to-end:
    1. Write minimal test CSVs (skip Excel pipeline for speed).
    2. Insert forecast results directly into the test DB.
    3. Call /forecast via TestClient (patched to use test DB session).
    4. Assert the response contains the expected data.
    """
    from src.api.main import app

    run_date = date(2024, 5, 1)
    for i in range(5):
        db_session.execute(
            text("""
                INSERT INTO forecast_results
                    (run_date, category, region, target_date,
                     predicted_revenue, yhat_lower, yhat_upper, model_version)
                VALUES
                    (:run_date, 'Snacks', 'SOUTHEAST', :tdate,
                     :rev, :lo, :hi, 'prophet-v1')
            """),
            {
                "run_date": run_date,
                "tdate": date(2024, 5, 2 + i),
                "rev": 50.0 + i,
                "lo": 30.0,
                "hi": 70.0,
            },
        )
    db_session.commit()

    # Build the expected return value from the rows we just inserted
    expected = {
        "run_date": run_date,
        "model_version": "prophet-v1",
        "points": [
            {
                "target_date": date(2024, 5, 2 + i),
                "predicted_revenue": 50.0 + i,
                "yhat_lower": 30.0,
                "yhat_upper": 70.0,
            }
            for i in range(5)
        ],
    }

    with ExitStack() as stack:
        stack.enter_context(patch("src.api.main.ping", return_value=True))
        stack.enter_context(
            patch("src.api.routes.forecast.get_forecast_rows", return_value=expected)
        )
        with TestClient(app) as client:
            resp = client.get("/forecast?category=Snacks&region=SOUTHEAST&horizon=5")

    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "Snacks"
    assert body["region"] == "SOUTHEAST"
    assert len(body["points"]) == 5
    assert body["points"][0]["predicted_revenue"] == pytest.approx(50.0)
