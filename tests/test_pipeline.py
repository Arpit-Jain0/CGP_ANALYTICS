"""
tests/test_pipeline.py

Unit tests for ingestion pipeline logic (no database, no network).

Scenarios
---------
- POS schema (Schema A): column_map renames source columns to canonical names.
- ONLINE schema (Schema B): column_map works without 'amount'; revenue absent.
- Currency normalisation: "$1,234.56" → 1234.56 numeric.
- Null repair: recognised null markers become pd.NA; all-null rows are dropped.
- Idempotent overwrite: running the same file group twice with write_mode="overwrite"
  yields the same row count (not doubled).
- File-level pipeline: an Excel file in a temp dir produces the expected CSV.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.ingestion.config_loader import (
    FileGroup,
    IngestionConfig,
    PipelineSettings,
    SheetConfig,
)
from src.ingestion.pipeline import (
    apply_sheet_config,
    clean_dataframe,
    run_pipeline,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _default_settings():
    return PipelineSettings(
        null_values=["", "NA", "N/A", "NULL", "None", "nan", "NaN"],
        date_formats=["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"],
    )


# ── POS schema mapping ─────────────────────────────────────────────────────────


def test_pos_schema_mapping():
    """POS source columns are renamed to canonical names via column_map."""
    df = pd.DataFrame(
        {
            "transaction_id": ["T001", "T002"],
            "ts": ["2024-01-10 08:00:00", "2024-01-11 09:00:00"],
            "store_id": ["S01", "S02"],
            "sku": ["SKU01", "SKU02"],
            "qty": [2, 3],
            "unit_price": [10.0, 20.0],
            "amount": [20.0, 60.0],
            "currency": ["USD", "USD"],
        }
    )
    sheet_cfg = SheetConfig(
        sheet="*",
        target_csv="sales_transactions.csv",
        column_map={
            "transaction_id": "transaction_id",
            "ts": "transaction_ts",
            "store_id": "store_id",
            "sku": "sku",
            "qty": "quantity",
            "unit_price": "unit_price",
            "amount": "revenue",
            "currency": "currency",
        },
        add_columns={"source_system": "POS"},
    )
    result = apply_sheet_config(df.copy(), sheet_cfg, "pos_2024.xlsx")

    expected_cols = {
        "transaction_id",
        "transaction_ts",
        "store_id",
        "sku",
        "quantity",
        "unit_price",
        "revenue",
        "currency",
        "source_system",
    }
    assert set(result.columns) == expected_cols
    assert list(result["source_system"]) == ["POS", "POS"]
    assert "qty" not in result.columns
    assert "amount" not in result.columns


# ── ONLINE schema mapping ──────────────────────────────────────────────────────


def test_online_schema_mapping():
    """
    ONLINE (Schema B) has no 'amount' column; the mapping omits revenue so the
    canonical CSV has the column absent.  Downstream derivation fills it in.
    """
    df = pd.DataFrame(
        {
            "order_id": ["O001", "O002"],
            "order_datetime": ["2024-01-10 10:00:00", "2024-01-11 11:00:00"],
            "location_id": ["S01", "S02"],
            "product_sku": ["SKU01", "SKU02"],
            "units": [1, 2],
            "price_per_unit": [15.0, 25.0],
            "currency": ["USD", "USD"],
        }
    )
    sheet_cfg = SheetConfig(
        sheet="*",
        target_csv="sales_transactions.csv",
        column_map={
            "order_id": "transaction_id",
            "order_datetime": "transaction_ts",
            "location_id": "store_id",
            "product_sku": "sku",
            "units": "quantity",
            "price_per_unit": "unit_price",
            "currency": "currency",
        },
        add_columns={"source_system": "ONLINE"},
    )
    result = apply_sheet_config(df.copy(), sheet_cfg, "online_2024.xlsx")

    assert "transaction_id" in result.columns
    assert "revenue" not in result.columns  # no 'amount' source → no revenue col
    assert list(result["source_system"]) == ["ONLINE", "ONLINE"]


# ── Currency normalisation ─────────────────────────────────────────────────────


def test_currency_string_normalised():
    """Currency-formatted strings like '$1,234.56' must coerce to numeric."""
    df = pd.DataFrame(
        {
            "amount": ["$1,234.56", "€2,000.00", "3500"],
            "store_id": ["S01", "S02", "S03"],  # ID column — must NOT coerce
        }
    )
    result = clean_dataframe(df.copy(), _default_settings())

    assert pd.api.types.is_numeric_dtype(result["amount"]), "amount should be numeric"
    assert float(result["amount"].iloc[0]) == pytest.approx(1234.56)
    assert float(result["amount"].iloc[1]) == pytest.approx(2000.00)
    # store_id is an ID column — must stay as a string dtype (object or StringDtype)
    assert pd.api.types.is_string_dtype(result["store_id"])


def test_whitespace_stripped_from_strings():
    """Leading/trailing whitespace in string cells is removed."""
    df = pd.DataFrame({"name": ["  Alice  ", "Bob "], "value": [1, 2]})
    result = clean_dataframe(df.copy(), _default_settings())
    assert result["name"].tolist() == ["Alice", "Bob"]


# ── Null repair ────────────────────────────────────────────────────────────────


def test_null_markers_become_na():
    """Recognised null markers (N/A, NULL, etc.) must become pd.NA after cleaning."""
    df = pd.DataFrame(
        {
            "txn_id": ["T001", "T002", "T003"],
            "amount": ["100", "NULL", "N/A"],
            "store": ["S01", "NA", "S03"],
        }
    )
    result = clean_dataframe(df.copy(), _default_settings())

    assert pd.isna(result["amount"].iloc[1])
    assert pd.isna(result["amount"].iloc[2])
    assert pd.isna(result["store"].iloc[1])


def test_all_null_rows_dropped():
    """Rows where every column is null/NA after substitution are removed."""
    df = pd.DataFrame(
        {
            "txn_id": ["T001", "NULL", "T003"],
            "amount": [10, "NA", 30],
            "store": ["S1", "N/A", "S3"],
        }
    )
    result = clean_dataframe(df.copy(), _default_settings())
    # Row index 1 is all-null → should be gone
    assert len(result) == 2
    assert "T001" in result["txn_id"].values
    assert "T003" in result["txn_id"].values


# ── Date parsing ──────────────────────────────────────────────────────────────


def test_date_columns_parsed():
    """Columns containing 'date' in the name are coerced to datetime."""
    df = pd.DataFrame(
        {
            "transaction_date": ["2024-01-01", "2024-06-15"],
            "amount": [100, 200],
        }
    )
    result = clean_dataframe(df.copy(), _default_settings())
    assert pd.api.types.is_datetime64_any_dtype(result["transaction_date"])


# ── Idempotent overwrite ───────────────────────────────────────────────────────


def test_idempotent_overwrite(tmp_path, wb_factory):
    """
    Running the same file group twice with write_mode="overwrite" produces
    exactly the same row count both times (not a doubling on the second run).
    """
    data_dir = tmp_path / "data_input"
    data_dir.mkdir()
    out_dir = tmp_path / "downstream"

    # Create the Excel manually since wb_factory uses tmp_path (different dir)
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    for row in [["id", "value"], [1, 10], [2, 20], [3, 30]]:
        ws.append(row)
    (data_dir / "batch.xlsx").parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(data_dir / "batch.xlsx"))

    sheet_cfg = SheetConfig(
        sheet="Sales",
        target_csv="out.csv",
        write_mode="overwrite",
    )
    group = FileGroup(
        name="test_group",
        dir=str(data_dir.relative_to(tmp_path)),  # relative to root
        file_pattern="*.xlsx",
        sheets=[sheet_cfg],
    )
    config = IngestionConfig(
        settings=PipelineSettings(downstream_dir=str(out_dir.relative_to(tmp_path))),
        file_groups=[group],
    )

    run_pipeline(tmp_path, config)
    count_first = sum(1 for _ in (out_dir / "out.csv").open()) - 1  # minus header

    run_pipeline(tmp_path, config)
    count_second = sum(1 for _ in (out_dir / "out.csv").open()) - 1

    assert (
        count_first == count_second == 3
    ), f"Expected 3 rows both times; got {count_first} then {count_second}"


# ── File-level pipeline smoke test ───────────────────────────────────────────


def test_pipeline_end_to_end(tmp_path):
    """
    An Excel file with a column_map config produces the expected canonical CSV.
    """
    import openpyxl

    src_dir = tmp_path / "input"
    src_dir.mkdir()
    out_dir = tmp_path / "out"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    for row in [
        ["txn_id", "store", "sku", "qty", "price", "curr"],
        ["T001", "S01", "SKU01", 2, 15.0, "USD"],
        ["T002", "S02", "SKU02", 1, 30.0, "USD"],
    ]:
        ws.append(row)
    wb.save(str(src_dir / "sample.xlsx"))

    sheet_cfg = SheetConfig(
        sheet="Transactions",
        target_csv="sales.csv",
        write_mode="overwrite",
        column_map={
            "txn_id": "transaction_id",
            "store": "store_id",
            "sku": "sku",
            "qty": "quantity",
            "price": "unit_price",
            "curr": "currency",
        },
        add_columns={"source_system": "POS"},
    )
    group = FileGroup(
        name="sample",
        dir="input",
        file_pattern="*.xlsx",
        sheets=[sheet_cfg],
    )
    config = IngestionConfig(
        settings=PipelineSettings(downstream_dir="out"),
        file_groups=[group],
    )

    run_pipeline(tmp_path, config)

    out_csv = out_dir / "sales.csv"
    assert out_csv.exists()
    df = pd.read_csv(out_csv)
    assert list(df.columns) == [
        "transaction_id",
        "store_id",
        "sku",
        "quantity",
        "unit_price",
        "currency",
        "source_system",
    ]
    assert len(df) == 2
    assert df["transaction_id"].iloc[0] == "T001"
    assert df["source_system"].iloc[0] == "POS"
