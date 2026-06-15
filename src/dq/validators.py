"""
src/dq/validators.py

Pure validation and normalization functions.  Each function returns the
cleaned value (or None when the row must be rejected) plus an optional
DQEntry describing what happened.  A None entry means the value was clean.

action_taken semantics
----------------------
REPAIRED  — issue detected and corrected automatically (row still inserted)
FLAGGED   — issue detected; row inserted but marked for review
REJECTED  — row must not be inserted
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

# Static EUR → USD conversion rate (source: config; update via env var in
# a later sprint when live FX feed is wired)
EUR_TO_USD: float = 1.08

_TS_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y",
]


# ── DQ log entry ─────────────────────────────────────────────────────────────

@dataclass
class DQEntry:
    """One row for data_quality_log.  Batch-level fields filled by the loader."""

    record_identifier: str
    issue_type:        str
    field_name:        str
    raw_value:         str
    action_taken:      str          # REPAIRED | FLAGGED | REJECTED

    # Filled in by the loader right before DB insert
    load_batch_id: int | None = field(default=None, repr=False)
    load_type:     str | None = field(default=None, repr=False)
    source_system: str | None = field(default=None, repr=False)
    source_file:   str | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "load_batch_id":     self.load_batch_id,
            "load_type":         self.load_type,
            "source_system":     self.source_system,
            "source_file":       self.source_file,
            "record_identifier": self.record_identifier,
            "issue_type":        self.issue_type,
            "field_name":        self.field_name,
            "raw_value":         str(self.raw_value)[:500],
            "action_taken":      self.action_taken,
        }


# ── Null helpers ──────────────────────────────────────────────────────────────

def _is_null(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, pd.Timestamp):
        return pd.isna(val)
    if isinstance(val, float) and np.isnan(val):
        return True
    if isinstance(val, str) and val.strip().lower() in ("", "nan", "none", "null", "na"):
        return True
    return False


# ── Timestamp parser ──────────────────────────────────────────────────────────

def parse_timestamp(
    val: Any,
    field_name: str,
    record_id: str,
) -> tuple[datetime | None, DQEntry | None]:
    """
    Accept a datetime object, pandas Timestamp, date-only object, or a string
    in any of the known formats.  Returns (parsed_dt, entry).
    entry is non-None only when the value needed coercion (REPAIRED) or is
    unusable (REJECTED).
    """
    if isinstance(val, datetime):
        return val, None
    if isinstance(val, pd.Timestamp):
        return val.to_pydatetime(), None
    # date without time component
    if hasattr(val, "year") and not isinstance(val, (datetime, pd.Timestamp)):
        return datetime(val.year, val.month, val.day), None

    if _is_null(val):
        return None, DQEntry(record_id, "NULL_TIMESTAMP", field_name, str(val), "REJECTED")

    raw = str(val).strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt), DQEntry(
                record_id, "MIXED_DATE_FMT", field_name, raw, "REPAIRED"
            )
        except ValueError:
            continue

    return None, DQEntry(record_id, "UNPARSEABLE_TIMESTAMP", field_name, raw, "REJECTED")


# ── Quantity validator ────────────────────────────────────────────────────────

def validate_quantity(
    qty: Any,
    field_name: str,
    record_id: str,
) -> tuple[float | None, DQEntry | None]:
    """Zero or negative quantity → REJECTED.  Null → REJECTED."""
    if _is_null(qty):
        return None, DQEntry(record_id, "NULL_QUANTITY", field_name, str(qty), "REJECTED")

    try:
        v = float(qty)
    except (TypeError, ValueError):
        return None, DQEntry(record_id, "INVALID_QUANTITY", field_name, str(qty), "REJECTED")

    if v <= 0:
        return None, DQEntry(record_id, "ZERO_NEG_QTY", field_name, str(qty), "REJECTED")

    return v, None


# ── Currency normaliser ───────────────────────────────────────────────────────

def normalize_currency(
    currency: Any,
    unit_price: Any,
    revenue: Any,
    record_id: str,
) -> tuple[str, float | None, float | None, DQEntry | None]:
    """
    Normalise to USD.  EUR is converted at EUR_TO_USD (REPAIRED).
    Any other non-USD code is FLAGGED but values are kept unchanged.
    Returns (currency, unit_price, revenue, entry|None).
    """
    cur = "USD" if _is_null(currency) else str(currency).strip().upper()

    if cur == "USD":
        up  = None if _is_null(unit_price) else float(unit_price)
        rev = None if _is_null(revenue)    else float(revenue)
        return cur, up, rev, None

    if cur == "EUR":
        up  = round(float(unit_price) * EUR_TO_USD, 4) if not _is_null(unit_price) else None
        rev = round(float(revenue)    * EUR_TO_USD, 4) if not _is_null(revenue)    else None
        return "USD", up, rev, DQEntry(
            record_id, "EUR_CURRENCY", "currency", cur, "REPAIRED"
        )

    # Unknown currency — keep values, flag for review
    up  = None if _is_null(unit_price) else float(unit_price)
    rev = None if _is_null(revenue)    else float(revenue)
    return cur, up, rev, DQEntry(
        record_id, "UNKNOWN_CURRENCY", "currency", cur, "FLAGGED"
    )


# ── Revenue / unit_price derivation ──────────────────────────────────────────

def derive_revenue(
    quantity: float,
    unit_price: Any,
    revenue: Any,
    record_id: str,
) -> tuple[float | None, float | None, list[DQEntry]]:
    """
    Fill in the missing side when exactly one of (unit_price, revenue) is null.
    Both null → revenue stays None (row will fail final checks).
    Returns (unit_price, revenue, [entries]).
    """
    entries: list[DQEntry] = []
    up_null  = _is_null(unit_price)
    rev_null = _is_null(revenue)

    if not up_null and not rev_null:
        return float(unit_price), float(revenue), entries

    if up_null and rev_null:
        entries.append(DQEntry(record_id, "NULL_UNIT_PRICE", "unit_price", str(unit_price), "FLAGGED"))
        return None, None, entries

    if up_null:
        rev = float(revenue)
        derived = round(rev / quantity, 4) if quantity else None
        if derived is not None:
            entries.append(DQEntry(record_id, "NULL_UNIT_PRICE", "unit_price", str(unit_price), "REPAIRED"))
        return derived, rev, entries

    # revenue null — derive from unit_price × quantity
    up = float(unit_price)
    entries.append(DQEntry(record_id, "NULL_AMOUNT", "revenue", str(revenue), "REPAIRED"))
    return up, round(up * quantity, 4), entries


# ── FK existence checks ───────────────────────────────────────────────────────

def check_store_fk(
    store_id: Any,
    valid_stores: frozenset[str],
    record_id: str,
) -> DQEntry | None:
    if _is_null(store_id):
        return DQEntry(record_id, "NULL_STORE_ID", "store_id", str(store_id), "REJECTED")
    if str(store_id) not in valid_stores:
        return DQEntry(record_id, "UNKNOWN_STORE_ID", "store_id", str(store_id), "FLAGGED")
    return None


def check_sku_fk(
    sku: Any,
    valid_skus: frozenset[str],
    record_id: str,
) -> DQEntry | None:
    if _is_null(sku):
        return DQEntry(record_id, "NULL_SKU", "sku", str(sku), "REJECTED")
    if str(sku) not in valid_skus:
        return DQEntry(record_id, "UNKNOWN_SKU", "sku", str(sku), "FLAGGED")
    return None


# ── Row-level orchestrator ────────────────────────────────────────────────────

def validate_canonical_row(
    row: dict,
    valid_stores: frozenset[str],
    valid_skus: frozenset[str],
) -> tuple[dict | None, list[DQEntry]]:
    """
    Validate and normalise one canonical row (dict with canonical field names).

    Returns
    -------
    (cleaned_row, entries)
        cleaned_row is None when the row must be REJECTED entirely.
        entries accumulates all REPAIRED / FLAGGED / REJECTED events.
    """
    record_id  = str(row.get("transaction_id", "UNKNOWN"))
    entries:    list[DQEntry] = []
    reject      = False

    # ── Timestamp ──────────────────────────────────────────────────────────
    ts, e = parse_timestamp(row.get("transaction_ts"), "transaction_ts", record_id)
    if e:
        entries.append(e)
    if ts is None:
        reject = True
    row = {**row, "transaction_ts": ts}

    # ── Quantity ───────────────────────────────────────────────────────────
    qty, e = validate_quantity(row.get("quantity"), "quantity", record_id)
    if e:
        entries.append(e)
    if qty is None:
        reject = True
    row = {**row, "quantity": qty}

    # ── Currency → then derive revenue (needs valid currency for conversions)
    if not reject and qty is not None:
        cur, up, rev, e = normalize_currency(
            row.get("currency"),
            row.get("unit_price"),
            row.get("revenue"),
            record_id,
        )
        if e:
            entries.append(e)
        row = {**row, "currency": cur, "unit_price": up, "revenue": rev}

        up, rev, deriv = derive_revenue(qty, up, rev, record_id)
        entries.extend(deriv)
        row = {**row, "unit_price": up, "revenue": rev}
        if rev is None:
            reject = True

    # ── FK checks ─────────────────────────────────────────────────────────
    store_e = check_store_fk(row.get("store_id"), valid_stores, record_id)
    if store_e:
        entries.append(store_e)
        if store_e.action_taken == "REJECTED":
            reject = True
        else:
            # Nullify invalid store_id to avoid FK violation while still
            # flagging; original value is preserved in the DQ log entry.
            row = {**row, "store_id": None}

    # sku has no FK constraint in the schema — keep raw value, flag only
    sku_e = check_sku_fk(row.get("sku"), valid_skus, record_id)
    if sku_e:
        entries.append(sku_e)
        if sku_e.action_taken == "REJECTED":
            reject = True

    return (None if reject else row), entries
