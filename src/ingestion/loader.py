"""
src/ingestion/loader.py

Two-mode ingestion pipeline for the CPG Analytics Platform.

Usage
-----
    python -m src.ingestion.loader --mode historical  [--root .]
    python -m src.ingestion.loader --mode incremental [--root .]

Historical mode
    Loads dimension tables (dim_region, dim_store, dim_product as SCD-2 seed,
    seasonal_calendar), secondary feeds, and both historical sales files.

Incremental mode
    Processes every .xlsx in data/input/incremental/ oldest-first.
    Handles dedup (idempotent ON CONFLICT), late-arriving rows, SCD-2 product
    attribute changes, and multi-sheet workbooks (Orders + product_updates).

After each file: cleaned rows → data/output/processed/
                 DQ entries   → data/output/quality_reports/
                 source file  → data/output/archive/
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger
from sqlalchemy import text

# ── Internal imports (use absolute paths so -m invocation works) ──────────────
from src.common.config import get_settings
from src.common.db import get_session
from src.common.excel_io import read_workbook
from src.dq.validators import DQEntry, validate_canonical_row
from src.ingestion.mappings import CANONICAL_SALES_COLS, apply_mapping, detect_source_system

# ── Constants ─────────────────────────────────────────────────────────────────
_CHUNK = 500   # rows per executemany call


# ═══════════════════════════════════════════════════════════════════════════════
# TYPE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _to_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().upper() in ("TRUE", "1", "YES")
    return False


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD-BATCH AUDIT
# ═══════════════════════════════════════════════════════════════════════════════

def _open_batch(session, load_type: str, source_file: str, source_system: str) -> int:
    row = session.execute(
        text("""
            INSERT INTO load_batch
                (load_type, source_file, source_system, started_at,
                 rows_in, inserted, deduped, rejected, repaired, flagged, late_arriving)
            VALUES
                (:load_type, :source_file, :source_system, now(),
                 0, 0, 0, 0, 0, 0, 0)
            RETURNING load_batch_id
        """),
        {"load_type": load_type, "source_file": source_file, "source_system": source_system},
    )
    batch_id = row.scalar()
    session.flush()
    return batch_id


def _close_batch(session, batch_id: int, counts: dict) -> None:
    session.execute(
        text("""
            UPDATE load_batch
            SET finished_at   = now(),
                rows_in       = :rows_in,
                inserted      = :inserted,
                deduped       = :deduped,
                rejected      = :rejected,
                repaired      = :repaired,
                flagged       = :flagged,
                late_arriving = :late_arriving
            WHERE load_batch_id = :batch_id
        """),
        {"batch_id": batch_id, **counts},
    )


def _insert_dq_logs(session, entries: list[DQEntry]) -> None:
    if not entries:
        return
    dicts = [e.to_dict() for e in entries]
    for i in range(0, len(dicts), _CHUNK):
        session.execute(
            text("""
                INSERT INTO data_quality_log
                    (load_batch_id, ingested_at, load_type, source_system,
                     source_file, record_identifier, issue_type, field_name,
                     raw_value, action_taken)
                VALUES
                    (:load_batch_id, now(), :load_type, :source_system,
                     :source_file, :record_identifier, :issue_type, :field_name,
                     :raw_value, :action_taken)
            """),
            dicts[i : i + _CHUNK],
        )


def _enrich_entries(
    entries: list[DQEntry],
    batch_id: int,
    load_type: str,
    source_system: str,
    source_file: str,
) -> None:
    for e in entries:
        e.load_batch_id = batch_id
        e.load_type     = load_type
        e.source_system = source_system
        e.source_file   = source_file


# ═══════════════════════════════════════════════════════════════════════════════
# DIMENSION QUERY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _valid_stores(session) -> frozenset[str]:
    return frozenset(session.execute(text("SELECT store_id FROM dim_store")).scalars())


def _valid_skus(session) -> frozenset[str]:
    return frozenset(
        session.execute(text("SELECT sku FROM dim_product WHERE is_current = TRUE")).scalars()
    )


def _max_transaction_ts(session) -> datetime | None:
    return session.execute(
        text("SELECT MAX(transaction_ts) FROM sales_transactions")
    ).scalar()


# ═══════════════════════════════════════════════════════════════════════════════
# DIMENSION LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

def _load_dim_region(session, df: pd.DataFrame) -> int:
    df = df.rename(columns=lambda c: c.strip().lower())
    rows = df[["region", "population", "median_income_band", "climate_zone"]].to_dict("records")
    session.execute(
        text("""
            INSERT INTO dim_region (region, population, median_income_band, climate_zone)
            VALUES (:region, :population, :median_income_band, :climate_zone)
            ON CONFLICT (region) DO NOTHING
        """),
        rows,
    )
    return len(rows)


def _load_dim_store(session, df: pd.DataFrame) -> int:
    df = df.rename(columns=lambda c: c.strip().lower())
    rows = df[["store_id", "region", "city", "store_type"]].to_dict("records")
    session.execute(
        text("""
            INSERT INTO dim_store (store_id, region, city, store_type)
            VALUES (:store_id, :region, :city, :store_type)
            ON CONFLICT (store_id) DO NOTHING
        """),
        rows,
    )
    return len(rows)


def _load_seasonal_calendar(session, df: pd.DataFrame) -> int:
    df = df.rename(columns=lambda c: c.strip().lower())
    rows = [
        {
            "calendar_date": _to_date(r.get("calendar_date")),
            "season":        r.get("season"),
            "is_holiday":    _coerce_bool(r.get("is_holiday")),
            "holiday_name":  r.get("holiday_name"),
        }
        for r in df.to_dict("records")
    ]
    session.execute(
        text("""
            INSERT INTO seasonal_calendar (calendar_date, season, is_holiday, holiday_name)
            VALUES (:calendar_date, :season, :is_holiday, :holiday_name)
            ON CONFLICT (calendar_date) DO NOTHING
        """),
        rows,
    )
    return len(rows)


def _init_dim_product(session, df: pd.DataFrame) -> int:
    """
    SCD-2 initial seed.  Each SKU gets one row with:
      valid_from  = launch_date (or today if absent)
      valid_to    = NULL
      is_current  = TRUE
    Idempotent: skips SKUs that already have a current row.
    """
    df = df.rename(columns=lambda c: c.strip().lower())
    inserted = 0
    for r in df.to_dict("records"):
        sku = str(r["sku"])
        existing = session.execute(
            text("SELECT 1 FROM dim_product WHERE sku = :sku AND is_current = TRUE"),
            {"sku": sku},
        ).scalar()
        if existing:
            continue
        launch = _to_date(r.get("launch_date"))
        session.execute(
            text("""
                INSERT INTO dim_product
                    (sku, category, brand, package_size, list_price,
                     launch_date, valid_from, valid_to, is_current)
                VALUES
                    (:sku, :category, :brand, :package_size, :list_price,
                     :launch_date, :valid_from, NULL, TRUE)
            """),
            {
                "sku":          sku,
                "category":     r.get("category"),
                "brand":        r.get("brand"),
                "package_size": r.get("package_size"),
                "list_price":   _safe_float(r.get("list_price")),
                "launch_date":  launch,
                "valid_from":   launch or date.today(),
            },
        )
        inserted += 1
    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# SECONDARY FEED LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

def _load_promo_windows(session, df: pd.DataFrame) -> int:
    df = df.rename(columns=lambda c: c.strip().lower())
    rows = [
        {
            "promo_id":    r["promo_id"],
            "category":    r.get("category"),
            "region":      r.get("region"),
            "start_date":  _to_date(r.get("start_date")),
            "end_date":    _to_date(r.get("end_date")),
            "discount_pct": _safe_float(r.get("discount_pct")),
        }
        for r in df.to_dict("records")
    ]
    session.execute(
        text("""
            INSERT INTO promo_windows
                (promo_id, category, region, start_date, end_date, discount_pct)
            VALUES (:promo_id, :category, :region, :start_date, :end_date, :discount_pct)
            ON CONFLICT (promo_id) DO NOTHING
        """),
        rows,
    )
    return len(rows)


def _load_campaigns(session, df: pd.DataFrame) -> int:
    df = df.rename(columns=lambda c: c.strip().lower())
    rows = [
        {
            "campaign_id": r["campaign_id"],
            "category":    r.get("category"),
            "region":      r.get("region"),
            "channel":     r.get("channel"),
            "start_date":  _to_date(r.get("start_date")),
            "end_date":    _to_date(r.get("end_date")),
            "exposure":    _safe_float(r.get("exposure")),
        }
        for r in df.to_dict("records")
    ]
    session.execute(
        text("""
            INSERT INTO marketing_campaigns
                (campaign_id, category, region, channel, start_date, end_date, exposure)
            VALUES (:campaign_id, :category, :region, :channel, :start_date, :end_date, :exposure)
            ON CONFLICT (campaign_id) DO NOTHING
        """),
        rows,
    )
    return len(rows)


def _load_competitor_prices(session, df: pd.DataFrame) -> int:
    df = df.rename(columns=lambda c: c.strip().lower())
    rows = [
        {
            "obs_date":          _to_date(r.get("obs_date")),
            "category":          r.get("category"),
            "region":            r.get("region"),
            "competitor_price":  _safe_float(r.get("competitor_price")),
        }
        for r in df.to_dict("records")
    ]
    session.execute(
        text("""
            INSERT INTO competitor_prices (obs_date, category, region, competitor_price)
            VALUES (:obs_date, :category, :region, :competitor_price)
            ON CONFLICT (obs_date, category, region) DO NOTHING
        """),
        rows,
    )
    return len(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# SCD-2 ATTRIBUTE CHANGE HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

def _process_scd2_updates(session, df: pd.DataFrame) -> int:
    """
    For each row in a product_updates sheet:
      1. Find the current dim_product row for the SKU.
      2. If the attribute value has changed, close the old row and insert a new one.
    Returns the number of SCD-2 updates applied.
    """
    df = df.rename(columns=lambda c: c.strip().lower())
    updates = 0

    for r in df.to_dict("records"):
        sku       = str(r.get("sku", "")).strip()
        attr      = str(r.get("attribute", "")).strip()
        new_val   = r.get("new_value")
        eff_date  = _to_date(r.get("effective_date")) or date.today()

        if not sku or not attr or new_val is None:
            logger.warning("SCD-2: skipping malformed product_updates row: {}", r)
            continue

        # Only list_price and package_size are supported here
        if attr not in ("list_price", "package_size", "category", "brand"):
            logger.warning("SCD-2: unsupported attribute '{}' for SKU {}", attr, sku)
            continue

        current = session.execute(
            text("SELECT * FROM dim_product WHERE sku = :sku AND is_current = TRUE"),
            {"sku": sku},
        ).mappings().first()

        if current is None:
            logger.warning("SCD-2: no current row found for SKU '{}'", sku)
            continue

        current = dict(current)
        old_val = str(current.get(attr, ""))

        # Normalise numeric comparison for list_price
        if attr == "list_price":
            try:
                changed = abs(float(old_val) - float(new_val)) > 1e-6
            except (TypeError, ValueError):
                changed = old_val != str(new_val)
        else:
            changed = old_val.strip() != str(new_val).strip()

        if not changed:
            logger.debug("SCD-2: SKU {} attr '{}' unchanged — skip", sku, attr)
            continue

        # Close old row
        session.execute(
            text("""
                UPDATE dim_product
                SET valid_to = :valid_to, is_current = FALSE
                WHERE product_key = :pk
            """),
            {"valid_to": eff_date - timedelta(days=1), "pk": current["product_key"]},
        )

        # Build new row inheriting all attributes from old
        new_row = {k: v for k, v in current.items() if k != "product_key"}
        if attr == "list_price":
            new_row["list_price"] = float(new_val)
        else:
            new_row[attr] = str(new_val)
        new_row["valid_from"] = eff_date
        new_row["valid_to"]   = None
        new_row["is_current"] = True

        session.execute(
            text("""
                INSERT INTO dim_product
                    (sku, category, brand, package_size, list_price,
                     launch_date, valid_from, valid_to, is_current)
                VALUES
                    (:sku, :category, :brand, :package_size, :list_price,
                     :launch_date, :valid_from, :valid_to, :is_current)
            """),
            new_row,
        )
        updates += 1
        logger.info(
            "SCD-2: SKU {} | {} changed: '{}' → '{}' (eff {})",
            sku, attr, old_val, new_val, eff_date,
        )

    return updates


# ═══════════════════════════════════════════════════════════════════════════════
# SALES TRANSACTION PROCESSOR
# ═══════════════════════════════════════════════════════════════════════════════

def _process_sales_df(
    session,
    df: pd.DataFrame,
    source_system: str,
    source_file: str,
    batch_id: int,
    load_type: str,
    max_ts: datetime | None,
    v_stores: frozenset[str],
    v_skus: frozenset[str],
) -> tuple[int, int, int, int, int, int, list[dict], list[DQEntry]]:
    """
    Map → validate → insert one sales DataFrame.

    Returns
    -------
    (rows_in, inserted, deduped, rejected, repaired, late_arriving,
     clean_rows_for_export, all_dq_entries)
    """
    all_dq:       list[DQEntry] = []
    clean_rows:   list[dict]    = []
    rejected_ids: set[str]      = set()
    repaired_ids: set[str]      = set()
    flagged_ids:  set[str]      = set()

    rows_in = len(df)

    # ── Validate every row ─────────────────────────────────────────────────
    for raw in df.to_dict("records"):
        cleaned, entries = validate_canonical_row(raw, v_stores, v_skus)
        for e in entries:
            e.load_batch_id = batch_id
            e.load_type     = load_type
            e.source_system = source_system
            e.source_file   = source_file
        all_dq.extend(entries)

        rid = str(raw.get("transaction_id", "UNKNOWN"))
        if cleaned is None:
            rejected_ids.add(rid)
        else:
            # Classify repaired vs flagged
            for e in entries:
                if e.action_taken == "REPAIRED":
                    repaired_ids.add(rid)
                elif e.action_taken == "FLAGGED":
                    flagged_ids.add(rid)
            clean_rows.append(cleaned)

    rejected = len(rejected_ids)

    # ── Dedup check (pre-filter to avoid spurious conflicts) ───────────────
    candidate_ids = [r["transaction_id"] for r in clean_rows]
    existing: set[str] = set()
    for i in range(0, len(candidate_ids), 1000):
        chunk = candidate_ids[i : i + 1000]
        res = session.execute(
            text("""
                SELECT transaction_id FROM sales_transactions
                WHERE transaction_id = ANY(:ids)
            """),
            {"ids": chunk},
        ).scalars()
        existing.update(res)

    new_rows:      list[dict] = []
    late_arriving  = 0
    for r in clean_rows:
        if r["transaction_id"] in existing:
            continue
        r["load_batch_id"]  = batch_id
        r["source_system"]  = source_system
        ts = r.get("transaction_ts")
        late = bool(max_ts and isinstance(ts, datetime) and ts < max_ts)
        r["is_late_arriving"] = late
        if late:
            late_arriving += 1
        new_rows.append(r)

    deduped  = len(clean_rows) - len(new_rows)
    inserted = len(new_rows)

    # ── Bulk insert ────────────────────────────────────────────────────────
    for i in range(0, len(new_rows), _CHUNK):
        batch = new_rows[i : i + _CHUNK]
        session.execute(
            text("""
                INSERT INTO sales_transactions
                    (transaction_id, transaction_ts, store_id, sku, quantity,
                     unit_price, revenue, currency, source_system,
                     load_batch_id, is_late_arriving)
                VALUES
                    (:transaction_id, :transaction_ts, :store_id, :sku, :quantity,
                     :unit_price, :revenue, :currency, :source_system,
                     :load_batch_id, :is_late_arriving)
                ON CONFLICT (transaction_id) DO NOTHING
            """),
            batch,
        )

    repaired = len(repaired_ids)
    flagged  = len(flagged_ids - repaired_ids)   # mutually exclusive display

    return rows_in, inserted, deduped, rejected, repaired, late_arriving, new_rows, all_dq


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT STAGE
# ═══════════════════════════════════════════════════════════════════════════════

def _export_processed(rows: list[dict], path: Path) -> None:
    if rows:
        pd.DataFrame(rows).to_excel(str(path), index=False, engine="openpyxl")
        logger.info("Processed export → {}", path.name)


def _export_quality_report(entries: list[DQEntry], path: Path) -> None:
    dicts = [e.to_dict() for e in entries]
    df = pd.DataFrame(dicts) if dicts else pd.DataFrame(
        columns=["load_batch_id","issue_type","field_name","raw_value","action_taken"]
    )
    df.to_excel(str(path), index=False, engine="openpyxl")
    logger.info("Quality report → {} ({} entries)", path.name, len(dicts))


def _archive(src: Path, archive_dir: Path) -> None:
    ts_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = archive_dir / src.name
    if dest.exists():
        dest = archive_dir / f"{src.stem}_{ts_suffix}{src.suffix}"
    src.rename(dest)
    logger.info("Archived {} → {}", src.name, dest.name)


# ═══════════════════════════════════════════════════════════════════════════════
# HISTORICAL BULK LOAD
# ═══════════════════════════════════════════════════════════════════════════════

def load_historical(root: Path) -> None:
    cfg        = get_settings()
    hist_dir   = root / cfg.data_input_historical
    arch_dir   = root / cfg.data_output_archive
    proc_dir   = root / cfg.data_output_processed
    qr_dir     = root / cfg.data_output_quality_reports
    for d in (arch_dir, proc_dir, qr_dir):
        d.mkdir(parents=True, exist_ok=True)

    # ── 1. Load dimension tables ────────────────────────────────────────────
    ref_path = hist_dir / "historical_data.xlsx"
    if not ref_path.exists():
        raise FileNotFoundError(f"Reference workbook not found: {ref_path}")

    logger.info("Loading reference dimensions from {}", ref_path.name)
    ref_sheets = read_workbook(ref_path)

    with get_session() as session:
        batch_id = _open_batch(session, "HISTORICAL", ref_path.name, "REFERENCE")
        n_reg  = _load_dim_region(session, ref_sheets["dim_region"])
        n_sto  = _load_dim_store(session, ref_sheets["dim_store"])
        n_cal  = _load_seasonal_calendar(session, ref_sheets["seasonal_calendar"])
        n_prod = _init_dim_product(session, ref_sheets["dim_product"])
        _close_batch(session, batch_id, {
            "rows_in": n_reg + n_sto + n_cal + n_prod,
            "inserted": n_reg + n_sto + n_cal + n_prod,
            "deduped": 0, "rejected": 0, "repaired": 0,
            "flagged": 0, "late_arriving": 0,
        })
        logger.info(
            "Dims loaded — regions:{} stores:{} calendar:{} products:{}",
            n_reg, n_sto, n_cal, n_prod,
        )

    # ── 2. Secondary feeds ──────────────────────────────────────────────────
    _feed_map = {
        "promo_windows.xlsx":       ("PromoWindows", _load_promo_windows),
        "marketing_campaigns.xlsx": ("Campaigns",    _load_campaigns),
        "competitor_prices.xlsx":   ("CompetitorPrices", _load_competitor_prices),
    }
    for fname, (sheet_hint, loader_fn) in _feed_map.items():
        fpath = hist_dir / fname
        if not fpath.exists():
            logger.warning("Secondary feed not found: {} — skipping", fname)
            continue
        sheets = read_workbook(fpath)
        df     = next(iter(sheets.values()))   # single-sheet files
        with get_session() as session:
            batch_id = _open_batch(session, "HISTORICAL", fname, fname.split("_")[0].upper())
            n = loader_fn(session, df)
            _close_batch(session, batch_id, {
                "rows_in": n, "inserted": n, "deduped": 0, "rejected": 0,
                "repaired": 0, "flagged": 0, "late_arriving": 0,
            })
            logger.info("Loaded {} rows from {}", n, fname)

    # ── 3. Historical sales transactions ────────────────────────────────────
    sales_files = [
        (hist_dir / "pos_sales_history.xlsx",    "Sales",  "POS"),
        (hist_dir / "online_sales_history.xlsx", "Orders", "ONLINE"),
    ]

    for fpath, sheet_name, source_system in sales_files:
        if not fpath.exists():
            logger.warning("Sales file not found: {} — skipping", fpath.name)
            continue

        logger.info("Processing {} ({})", fpath.name, source_system)
        sheets = read_workbook(fpath, target_sheets=[sheet_name])
        df_raw = sheets.get(sheet_name)
        if df_raw is None or df_raw.empty:
            logger.warning("Sheet '{}' empty in {} — skipping", sheet_name, fpath.name)
            continue

        df_mapped = apply_mapping(df_raw, source_system, fpath)

        with get_session() as session:
            v_stores = _valid_stores(session)
            v_skus   = _valid_skus(session)
            # Late-arriving detection is only meaningful for incremental loads;
            # passing None disables it for the historical bulk load.
            batch_id = _open_batch(session, "HISTORICAL", fpath.name, source_system)

            (rows_in, inserted, deduped, rejected,
             repaired, late_arr, new_rows, dq_entries) = _process_sales_df(
                session, df_mapped, source_system, fpath.name,
                batch_id, "HISTORICAL", max_ts=None, v_stores=v_stores, v_skus=v_skus,
            )

            _insert_dq_logs(session, dq_entries)
            _close_batch(session, batch_id, {
                "rows_in": rows_in, "inserted": inserted, "deduped": deduped,
                "rejected": rejected, "repaired": repaired,
                "flagged": sum(
                    1 for e in dq_entries
                    if e.action_taken == "FLAGGED"
                    and e.record_identifier not in {
                        ex.record_identifier for ex in dq_entries
                        if ex.action_taken == "REJECTED"
                    }
                ),
                "late_arriving": late_arr,
            })

        stem = fpath.stem
        _export_processed(new_rows, proc_dir / f"{stem}_batch{batch_id}.xlsx")
        _export_quality_report(dq_entries, qr_dir / f"{stem}_batch{batch_id}_quality.xlsx")

        logger.info(
            "{}: rows_in={} inserted={} deduped={} rejected={} repaired={} late={}",
            fpath.name, rows_in, inserted, deduped, rejected, repaired, late_arr,
        )

    # ── Archive all historical input files ──────────────────────────────────
    for f in hist_dir.glob("*.xlsx"):
        _archive(f, arch_dir)

    logger.info("Historical load complete.")


# ═══════════════════════════════════════════════════════════════════════════════
# INCREMENTAL BATCH PROCESSOR
# ═══════════════════════════════════════════════════════════════════════════════

def process_incremental(root: Path) -> None:
    cfg      = get_settings()
    incr_dir = root / cfg.data_input_incremental
    arch_dir = root / cfg.data_output_archive
    proc_dir = root / cfg.data_output_processed
    qr_dir   = root / cfg.data_output_quality_reports
    for d in (arch_dir, proc_dir, qr_dir):
        d.mkdir(parents=True, exist_ok=True)

    batch_files = sorted(incr_dir.glob("*.xlsx"))
    if not batch_files:
        logger.info("No incremental files found in {}", incr_dir)
        return

    for fpath in batch_files:
        logger.info("── Incremental batch: {}", fpath.name)
        sheets = read_workbook(fpath)     # all non-ghost sheets

        if not sheets:
            logger.warning("No readable sheets in {} — skipping", fpath.name)
            continue

        # Determine which sheets are sales vs product_updates
        sales_df:   pd.DataFrame | None = None
        source_sys: str = "UNKNOWN"
        scd2_df:    pd.DataFrame | None = None

        for sname, df in sheets.items():
            if sname.lower() == "product_updates":
                scd2_df = df
                logger.info("Found product_updates sheet ({} rows)", len(df))
            else:
                # Treat as a sales sheet — detect source system
                try:
                    ss = detect_source_system(fpath, sname, df)
                    sales_df   = apply_mapping(df, ss, fpath)
                    source_sys = ss
                    logger.info(
                        "Sales sheet '{}' detected as {} ({} rows)",
                        sname, ss, len(df),
                    )
                except ValueError as exc:
                    logger.warning("Cannot map sheet '{}': {} — skipping", sname, exc)

        all_dq:   list[DQEntry] = []
        all_new:  list[dict]    = []
        batch_id: int | None    = None

        with get_session() as session:
            v_stores = _valid_stores(session)
            v_skus   = _valid_skus(session)
            max_ts   = _max_transaction_ts(session)

            batch_id = _open_batch(session, "INCREMENTAL", fpath.name, source_sys)

            # ── SCD-2 updates (process before sales so new SKU attrs are live)
            scd2_count = 0
            if scd2_df is not None:
                scd2_count = _process_scd2_updates(session, scd2_df)
                if scd2_count:
                    # Refresh valid SKUs after SCD-2 (new rows may have new keys)
                    v_skus = _valid_skus(session)

            # ── Sales rows ─────────────────────────────────────────────────
            rows_in = inserted = deduped = rejected = repaired = late_arr = 0
            if sales_df is not None and not sales_df.empty:
                (rows_in, inserted, deduped, rejected,
                 repaired, late_arr, new_rows, dq_entries) = _process_sales_df(
                    session, sales_df, source_sys, fpath.name,
                    batch_id, "INCREMENTAL", max_ts, v_stores, v_skus,
                )
                all_dq.extend(dq_entries)
                all_new.extend(new_rows)

                flagged = sum(
                    1 for e in dq_entries
                    if e.action_taken == "FLAGGED"
                )

                _insert_dq_logs(session, dq_entries)
            else:
                flagged = 0

            _close_batch(session, batch_id, {
                "rows_in": rows_in, "inserted": inserted, "deduped": deduped,
                "rejected": rejected, "repaired": repaired,
                "flagged": flagged, "late_arriving": late_arr,
            })

        stem = fpath.stem
        if batch_id is not None:
            _export_processed(all_new, proc_dir / f"{stem}_batch{batch_id}.xlsx")
            _export_quality_report(all_dq, qr_dir / f"{stem}_batch{batch_id}_quality.xlsx")

        logger.info(
            "{}: rows_in={} inserted={} deduped={} rejected={} "
            "repaired={} late={} scd2_updates={}",
            fpath.name, rows_in, inserted, deduped,
            rejected, repaired, late_arr, scd2_count if scd2_df is not None else 0,
        )

        _archive(fpath, arch_dir)

    logger.info("Incremental processing complete ({} files).", len(batch_files))


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="CPG Analytics ingestion pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["historical", "incremental"],
        required=True,
        help="historical: bulk load from data/input/historical/  "
             "incremental: process batches in data/input/incremental/",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repo root directory (default: current working directory)",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()

    logger.remove()
    logger.add(sys.stderr, level=get_settings().log_level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    if args.mode == "historical":
        load_historical(root)
    else:
        process_incremental(root)


if __name__ == "__main__":
    _cli()
