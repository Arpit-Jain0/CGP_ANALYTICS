"""
src/api/queries.py

Data-access helpers for the API layer.  All heavy lifting lives here so
routes stay thin.  Two backends:

  CSV  — reads downstream CSVs for revenue aggregations (summary, insights, ask).
  DB   — queries Postgres for quality logs, load_batch, and forecast_results.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from src.common.db import get_session
from src.common.config import get_settings
from src.ingestion.config_loader import load_config

# ── Root resolution ───────────────────────────────────────────────────────────

# Resolves from src/api/ → src/ → project root
_APP_ROOT: Path = Path(__file__).resolve().parents[2]


def app_root() -> Path:
    return _APP_ROOT


def downstream_dir() -> Path:
    cfg_path = _APP_ROOT / get_settings().ingestion_config
    icfg = load_config(cfg_path)
    return _APP_ROOT / icfg.settings.downstream_dir


# ── CSV loaders ───────────────────────────────────────────────────────────────

def _load_sales() -> pd.DataFrame:
    """Return sales_transactions joined with category (dim_product) and region (dim_store)."""
    ds = downstream_dir()

    sales = pd.read_csv(ds / "sales_transactions.csv", low_memory=False)
    products = pd.read_csv(ds / "dim_product.csv")[["sku", "category"]].drop_duplicates("sku")
    stores = pd.read_csv(ds / "dim_store.csv")[["store_id", "region"]].drop_duplicates("store_id")

    sales["ds"] = pd.to_datetime(sales["transaction_ts"], errors="coerce").dt.normalize()

    for col in ("revenue", "unit_price", "quantity"):
        sales[col] = pd.to_numeric(sales[col], errors="coerce")

    # Derive revenue for ONLINE rows (no amount at source)
    missing = sales["revenue"].isna()
    sales.loc[missing, "revenue"] = (
        sales.loc[missing, "unit_price"] * sales.loc[missing, "quantity"]
    )

    sales = sales.merge(products, on="sku", how="left")
    sales = sales.merge(stores, on="store_id", how="left")
    return sales.dropna(subset=["ds", "revenue"])


# ── Summary queries ───────────────────────────────────────────────────────────

def get_revenue_kpis(
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    df = _load_sales()

    if start_date:
        df = df[df["ds"] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df["ds"] <= pd.Timestamp(end_date)]

    by_cat = df.groupby("category")["revenue"].sum().sort_values(ascending=False)
    by_reg = df.groupby("region")["revenue"].sum().sort_values(ascending=False)

    return {
        "total_revenue": round(float(df["revenue"].sum()), 2),
        "top_category": str(by_cat.index[0]) if len(by_cat) else "N/A",
        "top_region": str(by_reg.index[0]) if len(by_reg) else "N/A",
        "transaction_count": int(len(df)),
        "by_category": [
            {"category": str(k), "revenue": round(float(v), 2)} for k, v in by_cat.items()
        ],
        "by_region": [
            {"region": str(k), "revenue": round(float(v), 2)} for k, v in by_reg.items()
        ],
        "start_date": start_date,
        "end_date": end_date,
    }


# ── Quality queries (DB) ──────────────────────────────────────────────────────

def get_quality_summary() -> dict[str, Any]:
    with get_session() as session:
        issues = session.execute(
            text("""
                SELECT issue_type, count(*) AS cnt
                FROM data_quality_log
                GROUP BY issue_type
                ORDER BY cnt DESC
            """)
        ).fetchall()

        actions = session.execute(
            text("""
                SELECT action_taken, count(*) AS cnt
                FROM data_quality_log
                GROUP BY action_taken
                ORDER BY cnt DESC
            """)
        ).fetchall()

        total = session.execute(
            text("SELECT count(*) FROM data_quality_log")
        ).scalar()

        total_batches = session.execute(
            text("SELECT count(*) FROM load_batch")
        ).scalar()

        latest = session.execute(
            text("""
                SELECT load_batch_id, load_type, source_file,
                       rows_in, inserted, deduped, rejected,
                       repaired, flagged, late_arriving
                FROM load_batch
                ORDER BY load_batch_id DESC
                LIMIT 1
            """)
        ).fetchone()

    return {
        "total_issues": int(total or 0),
        "by_issue_type": [{"issue_type": r[0], "count": int(r[1])} for r in issues],
        "by_action_taken": [{"action_taken": r[0], "count": int(r[1])} for r in actions],
        "total_batches": int(total_batches or 0),
        "latest_batch": dict(latest._mapping) if latest else None,
    }


# ── Forecast queries (DB) ─────────────────────────────────────────────────────

def get_forecast_rows(
    category: str | None,
    region: str | None,
    horizon: int,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: dict[str, Any] = {"horizon": horizon}

    if category:
        clauses.append("category = :category")
        params["category"] = category
    if region:
        clauses.append("region = :region")
        params["region"] = region

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_session() as session:
        # Latest run_date for this filter
        run_row = session.execute(
            text(f"SELECT MAX(run_date) FROM forecast_results {where}"),
            params,
        ).scalar()

        if run_row is None:
            return {
                "run_date": None,
                "model_version": None,
                "points": [],
            }

        params["run_date"] = run_row

        run_filter = ("AND" if where else "WHERE") + " run_date = :run_date"

        version = session.execute(
            text(f"SELECT model_version FROM forecast_results {where} {run_filter} LIMIT 1"),
            params,
        ).scalar()

        rows = session.execute(
            text(f"""
                SELECT target_date, predicted_revenue, yhat_lower, yhat_upper
                FROM forecast_results
                {where} {run_filter}
                ORDER BY target_date
                LIMIT :horizon
            """),
            params,
        ).fetchall()

    return {
        "run_date": run_row,
        "model_version": version,
        "points": [
            {
                "target_date": r[0],
                "predicted_revenue": float(r[1]),
                "yhat_lower": float(r[2]) if r[2] is not None else None,
                "yhat_upper": float(r[3]) if r[3] is not None else None,
            }
            for r in rows
        ],
    }


# ── Ingest helpers ────────────────────────────────────────────────────────────

def _csv_row_counts(csv_paths: list[Path]) -> dict[str, int]:
    """Count data rows (excluding header) in each CSV that exists."""
    counts: dict[str, int] = {}
    for p in csv_paths:
        if p.exists():
            with p.open(encoding="utf-8") as f:
                counts[p.name] = max(0, sum(1 for _ in f) - 1)
    return counts


def run_ingest(mode: str) -> dict[str, Any]:
    """
    Run the ingestion pipeline filtered to the given mode, write a load_batch
    record to the DB, and return audit stats.

    mode = "historical" → groups whose dir contains "historical"
    mode = "incremental" → groups whose dir contains "incremental"
    """
    from datetime import datetime
    from src.ingestion.config_loader import load_config, IngestionConfig
    from src.ingestion.pipeline import run_pipeline

    root = app_root()
    cfg_path = root / get_settings().ingestion_config
    config = load_config(cfg_path)

    # Filter groups by mode keyword in their dir path
    matched = [g for g in config.file_groups if mode in g.dir.lower() and g.enabled]
    if not matched:
        return {
            "files_processed": 0,
            "inserted": 0,
            "load_batch_id": None,
        }

    filtered = IngestionConfig(settings=config.settings, file_groups=matched)

    # Target CSVs affected by this run
    ds = root / config.settings.downstream_dir
    target_csvs = list({
        ds / s.target_csv
        for g in matched
        for s in g.sheets
        if s.enabled
    })

    # Row snapshot before
    before = _csv_row_counts(target_csvs)

    # File count
    files_processed = sum(
        len(list((root / g.dir).glob(g.file_pattern)))
        for g in matched
        if (root / g.dir).exists()
    )

    started_at = datetime.utcnow()
    run_pipeline(root, filtered)
    finished_at = datetime.utcnow()

    # Delta rows
    after = _csv_row_counts(target_csvs)
    inserted = sum(
        max(0, after.get(p.name, 0) - before.get(p.name, 0))
        for p in target_csvs
    )

    # Write load_batch record
    load_batch_id: int | None = None
    try:
        with get_session() as session:
            result = session.execute(
                text("""
                    INSERT INTO load_batch
                        (load_type, source_file, started_at, finished_at, inserted)
                    VALUES
                        (:load_type, :source_file, :started_at, :finished_at, :inserted)
                    RETURNING load_batch_id
                """),
                {
                    "load_type": mode.upper(),
                    "source_file": f"{len(matched)} group(s)",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "inserted": inserted,
                },
            )
            load_batch_id = result.scalar()
    except Exception:
        pass   # DB write failure does not abort the ingest response

    return {
        "files_processed": files_processed,
        "inserted": inserted,
        "load_batch_id": load_batch_id,
    }


# ── Bounded context for /ask ──────────────────────────────────────────────────

def build_bounded_context() -> str:
    """
    Build a compact, pre-aggregated text context for the LLM.
    Never sends raw transaction rows — only summary statistics.
    """
    df = _load_sales()

    total_rev = df["revenue"].sum()
    date_min = df["ds"].min().date() if not df["ds"].isna().all() else "N/A"
    date_max = df["ds"].max().date() if not df["ds"].isna().all() else "N/A"

    by_cat = df.groupby("category")["revenue"].sum().sort_values(ascending=False)
    by_reg = df.groupby("region")["revenue"].sum().sort_values(ascending=False)

    # Monthly revenue (last 12 months)
    df["ym"] = df["ds"].dt.to_period("M")
    by_month = (
        df.groupby("ym")["revenue"]
        .sum()
        .sort_index()
        .tail(12)
    )

    # Month-over-month growth for category top-movers
    df_m = df.groupby(["ym", "category"])["revenue"].sum().reset_index()
    df_m["ym_str"] = df_m["ym"].astype(str)
    recent_months = sorted(df_m["ym_str"].unique())[-2:]
    movers: list[str] = []
    if len(recent_months) == 2:
        prev_m, curr_m = recent_months
        prev = df_m[df_m["ym_str"] == prev_m].set_index("category")["revenue"]
        curr = df_m[df_m["ym_str"] == curr_m].set_index("category")["revenue"]
        growth = ((curr - prev) / prev.replace(0, float("nan"))).dropna().sort_values()
        for cat, pct in growth.items():
            movers.append(f"  {cat}: {pct:+.1%}")

    # Quality summary from DB
    try:
        qs = get_quality_summary()
        quality_text = (
            f"Total quality issues logged: {qs['total_issues']}\n"
            f"Total ingestion batches: {qs['total_batches']}"
        )
    except Exception:
        quality_text = "Quality data unavailable"

    # Forecast summary from DB
    try:
        with get_session() as session:
            fc_count = session.execute(
                text("SELECT count(*) FROM forecast_results")
            ).scalar()
            fc_max = session.execute(
                text("SELECT MAX(target_date) FROM forecast_results")
            ).scalar()
        forecast_text = f"Forecast rows in DB: {fc_count} (latest target: {fc_max})"
    except Exception:
        forecast_text = "Forecast data unavailable"

    lines = [
        "=== CPG Analytics — Data Summary Context ===",
        f"Date range : {date_min} to {date_max}",
        f"Total revenue : ${total_rev:,.2f}",
        f"Total transactions : {len(df):,}",
        "",
        "Revenue by Category:",
        *[f"  {k}: ${v:,.2f} ({v/total_rev:.1%})" for k, v in by_cat.items()],
        "",
        "Revenue by Region:",
        *[f"  {k}: ${v:,.2f} ({v/total_rev:.1%})" for k, v in by_reg.items()],
        "",
        "Monthly Revenue (last 12 months):",
        *[f"  {str(period)}: ${val:,.2f}" for period, val in by_month.items()],
        "",
        "Category Revenue Growth (latest MoM):",
        *(movers if movers else ["  N/A"]),
        "",
        f"Data Quality: {quality_text}",
        f"Forecasting: {forecast_text}",
        "=" * 46,
    ]
    return "\n".join(lines)


def get_insights_aggregates() -> dict[str, Any]:
    """Return the aggregates needed for /insights (separate from bounded context)."""
    df = _load_sales()

    total = df["revenue"].sum()
    by_cat = df.groupby("category")["revenue"].sum().sort_values(ascending=False)
    by_reg = df.groupby("region")["revenue"].sum().sort_values(ascending=False)

    return {
        "total_revenue": round(float(total), 2),
        "by_category": [
            {"category": str(k), "revenue": round(float(v), 2)} for k, v in by_cat.items()
        ],
        "by_region": [
            {"region": str(k), "revenue": round(float(v), 2)} for k, v in by_reg.items()
        ],
    }
