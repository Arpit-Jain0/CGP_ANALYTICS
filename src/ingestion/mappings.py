"""
src/ingestion/mappings.py

Per-source column-name mappings from raw source schemas to the canonical
field names used throughout the pipeline and in the DB.

Canonical sales columns
-----------------------
transaction_id, transaction_ts, store_id, sku, quantity,
unit_price, revenue, currency

Schema A  (POS system)       — sheet "Sales"
Schema B  (Online / drifted) — sheet "Orders", NO revenue column
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger


# ── Canonical field set ───────────────────────────────────────────────────────

CANONICAL_SALES_COLS: tuple[str, ...] = (
    "transaction_id",
    "transaction_ts",
    "store_id",
    "sku",
    "quantity",
    "unit_price",
    "revenue",      # absent in schema B; filled with None, derived during DQ
    "currency",
)

# ── Source → canonical column maps ────────────────────────────────────────────

SCHEMA_POS: dict[str, str] = {
    "transaction_id": "transaction_id",
    "ts":             "transaction_ts",
    "store_id":       "store_id",
    "sku":            "sku",
    "qty":            "quantity",
    "unit_price":     "unit_price",
    "amount":         "revenue",
    "currency":       "currency",
}

SCHEMA_ONLINE: dict[str, str] = {
    "order_id":       "transaction_id",
    "order_datetime": "transaction_ts",
    "location_id":    "store_id",
    "product_sku":    "sku",
    "units":          "quantity",
    "price_per_unit": "unit_price",
    # no amount/revenue column in schema B
    "currency":       "currency",
}

_SCHEMAS: dict[str, dict[str, str]] = {
    "POS":    SCHEMA_POS,
    "ONLINE": SCHEMA_ONLINE,
}

# Sheet name (lower-cased) → source system
_SHEET_SOURCE: dict[str, str] = {
    "sales":  "POS",
    "orders": "ONLINE",
}


# ── Source-system detection ───────────────────────────────────────────────────

def detect_source_system(
    path: Path,
    sheet_name: str,
    df: pd.DataFrame,
) -> str:
    """
    Infer the source system for a sales sheet.  Tries (in order):
    1. Sheet name  2. File name stem  3. Column presence.
    Raises ValueError when no schema matches.
    """
    guess = _SHEET_SOURCE.get(sheet_name.strip().lower())
    if guess:
        return guess

    stem = path.stem.lower()
    if "pos" in stem:
        return "POS"
    if "online" in stem:
        return "ONLINE"

    cols = {c.strip().lower() for c in df.columns}
    if "transaction_id" in cols and "ts" in cols:
        return "POS"
    if "order_id" in cols and "order_datetime" in cols:
        return "ONLINE"

    raise ValueError(
        f"Cannot infer source schema for sheet='{sheet_name}' in '{path.name}'. "
        f"Columns found: {sorted(cols)}"
    )


# ── Column mapping ────────────────────────────────────────────────────────────

def apply_mapping(
    df: pd.DataFrame,
    source_system: str,
    path: Path,
) -> pd.DataFrame:
    """
    Rename source columns to canonical names and return a DataFrame containing
    exactly ``CANONICAL_SALES_COLS``.

    Unknown source columns are logged as warnings and dropped — they are never
    silently discarded without a log entry.  Missing canonical columns (e.g.
    revenue in schema B) are added as ``None``.
    """
    schema = _SCHEMAS.get(source_system)
    if schema is None:
        raise ValueError(f"No column mapping defined for source_system='{source_system}'")

    # Case-insensitive, whitespace-stripped comparison
    df = df.rename(columns=lambda c: c.strip().lower())
    schema_lc = {k.lower(): v for k, v in schema.items()}

    known   = [c for c in df.columns if c in schema_lc]
    unknown = [c for c in df.columns if c not in schema_lc]

    if unknown:
        logger.warning(
            "{} ({}): unrecognised columns {} — dropped (not in {} schema)",
            path.name, source_system, unknown, source_system,
        )

    renamed = df[known].rename(columns={c: schema_lc[c] for c in known})

    # Ensure all canonical columns are present
    for col in CANONICAL_SALES_COLS:
        if col not in renamed.columns:
            renamed[col] = None

    return renamed[list(CANONICAL_SALES_COLS)]
