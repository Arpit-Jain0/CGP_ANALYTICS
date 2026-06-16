"""
src/forecasting/forecaster.py

Prophet-based revenue forecaster for CPG Analytics.

For each (category, region) pair:
  1. Aggregates daily revenue from downstream CSV (sales_transactions.csv),
     joining through dim_product (category lookup) and dim_store (region lookup).
  2. Derives missing revenue for Online rows (unit_price × quantity).
  3. Feeds US-holiday effects from seasonal_calendar.csv into Prophet.
  4. Adds a promo regressor (discount_pct / 100) from promo_windows.csv so
     promo lift is modeled explicitly, not absorbed into residuals.
  5. Fits Prophet with weekly + yearly seasonality (multiplicative mode).
  6. Writes a horizon-day forecast to the forecast_results Postgres table,
     replacing any prior rows for the same run_date / category / region.

Extension points (data present, not yet wired as regressors):
    marketing_campaigns.csv  — channel spend; add as regressor in v2
    competitor_prices.csv    — weekly obs; add as regressor in v2

Usage
-----
    python -m src.forecasting.forecaster [--horizon 90] [--root .]
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger
from prophet import Prophet
from sqlalchemy import text

from src.common.config import get_settings
from src.common.db import get_session
from src.ingestion.config_loader import load_config

# Silence Prophet / cmdstanpy noise; loguru handles our own messages.
logging.getLogger("prophet").setLevel(logging.ERROR)
logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="Optimization terminated abnormally")
warnings.filterwarnings("ignore", message="No seasonality parameters")

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_HISTORY_DAYS: int = 60    # skip series shorter than this
MODEL_VERSION:    str = "prophet-v1"

_FC_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS forecast_results (
    forecast_id       SERIAL         PRIMARY KEY,
    run_date          DATE           NOT NULL,
    category          TEXT           NOT NULL,
    region            TEXT           NOT NULL,
    target_date       DATE           NOT NULL,
    predicted_revenue NUMERIC(14,4)  NOT NULL,
    yhat_lower        NUMERIC(14,4),
    yhat_upper        NUMERIC(14,4),
    model_version     TEXT           NOT NULL DEFAULT '1.0',
    created_at        TIMESTAMPTZ    NOT NULL DEFAULT now(),
    UNIQUE (run_date, category, region, target_date)
);
CREATE INDEX IF NOT EXISTS idx_fc_run    ON forecast_results (run_date, category, region);
CREATE INDEX IF NOT EXISTS idx_fc_target ON forecast_results (category, region, target_date);
"""

_CHUNK = 500   # rows per executemany call


# ── Setup helpers ─────────────────────────────────────────────────────────────

def _ensure_table(session) -> None:
    """Create forecast_results table + indexes if they do not exist."""
    for stmt in _FC_TABLE_DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            session.execute(text(stmt))


def _downstream_dir(root: Path) -> Path:
    """Resolve downstream CSV directory from the ingestion config."""
    cfg_path = root / get_settings().ingestion_config
    icfg = load_config(cfg_path)
    return root / icfg.settings.downstream_dir


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(downstream_dir: Path) -> dict[str, pd.DataFrame]:
    """
    Load the five CSVs needed for forecasting.
    Raises FileNotFoundError if any are absent — run the ingestion pipeline first.
    """
    files = {
        "sales":    "sales_transactions.csv",
        "products": "dim_product.csv",
        "stores":   "dim_store.csv",
        "calendar": "seasonal_calendar.csv",
        "promos":   "promo_windows.csv",
    }
    dfs: dict[str, pd.DataFrame] = {}
    for key, fname in files.items():
        path = downstream_dir / fname
        if not path.exists():
            raise FileNotFoundError(
                f"Required CSV not found: {path}\n"
                "Run the ingestion pipeline first:  "
                "python -m src.ingestion.pipeline"
            )
        dfs[key] = pd.read_csv(path)
        logger.debug("Loaded {}: {} rows", fname, len(dfs[key]))
    return dfs


# ── Daily series construction ─────────────────────────────────────────────────

def build_daily_series(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Return a DataFrame with columns (ds, category, region, y) where:
      ds  = calendar date (daily grain)
      y   = total revenue for that (date, category, region)

    Online transactions lack a revenue column at source; we derive it as
    unit_price × quantity before aggregating.
    """
    sales = dfs["sales"].copy()

    # Parse timestamps — normalize to midnight (daily grain)
    sales["ds"] = pd.to_datetime(sales["transaction_ts"], errors="coerce").dt.normalize()
    sales = sales.dropna(subset=["ds"])

    # Revenue derivation for Online rows
    for col in ("revenue", "unit_price", "quantity"):
        sales[col] = pd.to_numeric(sales[col], errors="coerce")
    missing_rev = sales["revenue"].isna()
    sales.loc[missing_rev, "revenue"] = (
        sales.loc[missing_rev, "unit_price"] * sales.loc[missing_rev, "quantity"]
    )

    # Join category via dim_product (one row per SKU; no SCD-2 in CSV)
    products = dfs["products"][["sku", "category"]].drop_duplicates("sku")
    sales = sales.merge(products, on="sku", how="left")

    # Join region via dim_store
    stores = dfs["stores"][["store_id", "region"]].drop_duplicates("store_id")
    sales = sales.merge(stores, on="store_id", how="left")

    # Drop rows we cannot place in a (category, region)
    sales = sales.dropna(subset=["category", "region", "revenue"])

    daily = (
        sales.groupby(["ds", "category", "region"])["revenue"]
        .sum()
        .reset_index()
        .rename(columns={"revenue": "y"})
        .sort_values("ds")
    )

    logger.info(
        "Daily series: {} rows across {} (category, region) pairs",
        len(daily),
        daily.groupby(["category", "region"]).ngroups,
    )
    return daily


# ── Holiday calendar ──────────────────────────────────────────────────────────

def build_holidays(calendar_df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert seasonal_calendar rows into Prophet's holidays DataFrame format.

    Prophet expects columns: holiday, ds, lower_window, upper_window.
    We include a one-day post-window to capture spending the day after.
    """
    cal = calendar_df.copy()
    cal["ds"] = pd.to_datetime(cal["calendar_date"], errors="coerce")
    # Handle both bool True and string "True"
    cal["is_holiday"] = cal["is_holiday"].apply(
        lambda x: x is True or str(x).strip().lower() == "true"
    )
    hol = cal[cal["is_holiday"]].copy()

    if hol.empty:
        logger.warning("No holidays found in seasonal_calendar — holiday effects disabled")
        return pd.DataFrame(columns=["holiday", "ds", "lower_window", "upper_window"])

    hol["holiday"] = hol["holiday_name"].fillna("US_Holiday").str.strip()
    hol["lower_window"] = 0
    hol["upper_window"] = 1   # extend effect one day past the holiday

    logger.info("Holiday calendar: {} holiday-days loaded", len(hol))
    return hol[["holiday", "ds", "lower_window", "upper_window"]].reset_index(drop=True)


# ── Promo regressor ───────────────────────────────────────────────────────────

def build_promo_feature(
    promo_df: pd.DataFrame,
    category: str,
    region: str,
    date_index: pd.DatetimeIndex,
) -> pd.Series:
    """
    Build a date-indexed Series of promo intensity for one (category, region).

    Values are discount_pct / 100 (e.g. 0.15 for a 15 % discount) so the
    regressor encodes both the existence AND the depth of a promotion.
    When multiple promos overlap, the highest discount wins.
    Days with no active promo are 0.0.
    """
    result = pd.Series(0.0, index=date_index, name="promo_active")

    mask = (
        (promo_df["category"].str.strip().str.lower() == category.lower()) &
        (promo_df["region"].str.strip().str.lower() == region.lower())
    )
    cat_promos = promo_df[mask].copy()
    if cat_promos.empty:
        return result

    cat_promos["start_date"] = pd.to_datetime(cat_promos["start_date"], errors="coerce")
    cat_promos["end_date"]   = pd.to_datetime(cat_promos["end_date"],   errors="coerce")
    cat_promos["discount_pct"] = pd.to_numeric(
        cat_promos["discount_pct"], errors="coerce"
    ).fillna(0)

    for _, row in cat_promos.iterrows():
        if pd.isna(row["start_date"]) or pd.isna(row["end_date"]):
            continue
        active = (date_index >= row["start_date"]) & (date_index <= row["end_date"])
        intensity = row["discount_pct"] / 100.0
        # Take max when promos overlap
        result[active] = result[active].clip(lower=intensity)

    return result


# ── Prophet fit + forecast ────────────────────────────────────────────────────

def fit_and_forecast(
    train_df: pd.DataFrame,
    holidays_df: pd.DataFrame,
    promo_lookup: dict,          # ds → promo_active value for full date range
    horizon_days: int,
) -> tuple[pd.DataFrame | None, str | None]:
    """
    Fit a Prophet model on *train_df* and return a horizon-day forecast.

    Parameters
    ----------
    train_df
        Must have columns: ds (datetime), y (float).
        promo_active column is added here from promo_lookup.
    holidays_df
        Prophet-format holidays DataFrame (may be empty).
    promo_lookup
        Mapping from Timestamp → promo intensity for both training and
        forecast period.  Keys must cover the full extended range.
    horizon_days
        Number of calendar days to forecast after the last training date.

    Returns
    -------
    (forecast_df, None)  on success; (None, error_message) on failure.
    forecast_df has columns: ds, yhat, yhat_lower, yhat_upper.
    Only future rows (ds > max training date) are returned.
    """
    train = train_df[["ds", "y"]].copy().sort_values("ds").reset_index(drop=True)
    train["ds"] = pd.to_datetime(train["ds"])

    # Attach promo feature for training period
    train["promo_active"] = train["ds"].map(promo_lookup).fillna(0.0)
    has_promo = train["promo_active"].sum() > 0

    m = Prophet(
        weekly_seasonality=True,
        yearly_seasonality=True,
        holidays=holidays_df if not holidays_df.empty else None,
        seasonality_mode="multiplicative",
        interval_width=0.90,
        changepoint_prior_scale=0.05,
        changepoint_range=0.90,
    )
    if has_promo:
        # Multiplicative mode: promo contribution scales with trend × seasonality,
        # matching the underlying demand model (demand_rate × promo_factor).
        m.add_regressor("promo_active", mode="multiplicative")

    try:
        fit_cols = ["ds", "y", "promo_active"] if has_promo else ["ds", "y"]
        m.fit(train[fit_cols])
    except Exception as exc:
        return None, str(exc)

    # Build future frame and attach promo regressor for the forecast period
    future = m.make_future_dataframe(periods=horizon_days, freq="D")
    if has_promo:
        future["promo_active"] = future["ds"].map(promo_lookup).fillna(0.0)

    try:
        forecast = m.predict(future)
    except Exception as exc:
        return None, f"predict failed: {exc}"

    # Keep only the future portion; clip negatives to 0 (revenue can't be negative)
    max_train = train["ds"].max()
    fut = forecast[forecast["ds"] > max_train][
        ["ds", "yhat", "yhat_lower", "yhat_upper"]
    ].copy()
    fut["yhat"]       = fut["yhat"].clip(lower=0.0)
    fut["yhat_lower"] = fut["yhat_lower"].clip(lower=0.0)
    fut["yhat_upper"] = fut["yhat_upper"].clip(lower=0.0)

    return fut, None


# ── DB write ──────────────────────────────────────────────────────────────────

def write_results(
    session,
    rows: list[dict[str, Any]],
    run_date: date,
    category: str,
    region: str,
) -> int:
    """
    Replace any prior forecast for this (run_date, category, region) and
    insert the new rows.  Returns the number of rows inserted.
    """
    session.execute(
        text("""
            DELETE FROM forecast_results
            WHERE run_date = :run_date
              AND category  = :category
              AND region    = :region
        """),
        {"run_date": run_date, "category": category, "region": region},
    )
    if not rows:
        return 0

    for i in range(0, len(rows), _CHUNK):
        session.execute(
            text("""
                INSERT INTO forecast_results
                    (run_date, category, region, target_date,
                     predicted_revenue, yhat_lower, yhat_upper, model_version)
                VALUES
                    (:run_date, :category, :region, :target_date,
                     :predicted_revenue, :yhat_lower, :yhat_upper, :model_version)
                ON CONFLICT (run_date, category, region, target_date) DO UPDATE
                    SET predicted_revenue = EXCLUDED.predicted_revenue,
                        yhat_lower        = EXCLUDED.yhat_lower,
                        yhat_upper        = EXCLUDED.yhat_upper,
                        model_version     = EXCLUDED.model_version,
                        created_at        = now()
            """),
            rows[i : i + _CHUNK],
        )
    return len(rows)


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_forecasts(
    root: Path = Path("."),
    horizon_days: int = 90,
    min_history_days: int = MIN_HISTORY_DAYS,
) -> None:
    """
    Main entry point.  Fits one Prophet model per (category, region) pair
    and writes horizon_days of predictions to forecast_results.

    Parameters
    ----------
    root
        Repo root directory.  Downstream CSVs are resolved via ingestion.json.
    horizon_days
        Calendar days to forecast beyond the last training date.
    min_history_days
        Pairs with fewer unique training dates than this are skipped.
    """
    root = Path(root).resolve()
    run_date = date.today()

    # ── Load CSVs ────────────────────────────────────────────────────────────
    ds_dir = _downstream_dir(root)
    logger.info("Reading data from {}", ds_dir)
    dfs = load_data(ds_dir)

    daily  = build_daily_series(dfs)
    hol_df = build_holidays(dfs["calendar"])

    # ── Ensure DB table exists ────────────────────────────────────────────────
    try:
        with get_session() as session:
            _ensure_table(session)
        logger.info("forecast_results table ready")
    except Exception as exc:
        logger.error("Cannot reach database: {}", exc)
        logger.error("Start Postgres:  docker compose up postgres -d")
        sys.exit(1)

    # ── Iterate pairs ─────────────────────────────────────────────────────────
    pairs = (
        daily.groupby(["category", "region"])
        .agg(n_days=("ds", "nunique"), last_date=("ds", "max"))
        .reset_index()
    )
    logger.info("Forecasting {} (category, region) pairs | horizon={} days", len(pairs), horizon_days)

    total_written = 0
    skipped = 0

    for _, p in pairs.iterrows():
        category = p["category"]
        region   = p["region"]
        n_days   = int(p["n_days"])

        if n_days < min_history_days:
            logger.warning(
                "SKIP ({}, {}) — only {} unique training dates (min {})",
                category, region, n_days, min_history_days,
            )
            skipped += 1
            continue

        # Training series for this pair
        series = daily[
            (daily["category"] == category) & (daily["region"] == region)
        ].copy()

        # Promo regressor — build for training + full forecast window
        first_date = series["ds"].min()
        last_date  = series["ds"].max()
        extended   = pd.date_range(
            start=first_date,
            end=last_date + timedelta(days=horizon_days),
            freq="D",
        )
        promo_feat   = build_promo_feature(dfs["promos"], category, region, extended)
        promo_lookup = dict(zip(extended, promo_feat.values))

        has_promo_in_train = promo_feat.loc[promo_feat.index <= last_date].sum() > 0

        logger.info(
            "({:14s}, {:10s})  {} training days | promo_days={}",
            category, region, n_days,
            int((promo_feat.loc[promo_feat.index <= last_date] > 0).sum()),
        )

        # Fit + forecast
        forecast, err = fit_and_forecast(series, hol_df, promo_lookup, horizon_days)
        if forecast is None:
            logger.error("  Prophet failed: {}", err)
            skipped += 1
            continue

        # Build DB rows
        db_rows = [
            {
                "run_date":          run_date,
                "category":          category,
                "region":            region,
                "target_date":       row["ds"].date(),
                "predicted_revenue": round(float(row["yhat"]),       4),
                "yhat_lower":        round(float(row["yhat_lower"]), 4),
                "yhat_upper":        round(float(row["yhat_upper"]), 4),
                "model_version":     MODEL_VERSION,
            }
            for _, row in forecast.iterrows()
        ]

        with get_session() as session:
            n = write_results(session, db_rows, run_date, category, region)

        total_written += n
        logger.info(
            "  wrote {} points | yhat {:.0f}–{:.0f} | interval [{:.0f}, {:.0f}]",
            n,
            forecast["yhat"].min(), forecast["yhat"].max(),
            forecast["yhat_lower"].min(), forecast["yhat_upper"].max(),
        )

    logger.info(
        "Done — {} pairs written ({} rows total), {} skipped",
        len(pairs) - skipped, total_written, skipped,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="CPG Analytics — Prophet revenue forecaster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m src.forecasting.forecaster\n"
            "  python -m src.forecasting.forecaster --horizon 180\n"
            "  python -m src.forecasting.forecaster --root /data/project\n"
        ),
    )
    parser.add_argument("--horizon",     type=int, default=90,
                        help="Days to forecast (default: 90)")
    parser.add_argument("--min-history", type=int, default=MIN_HISTORY_DAYS,
                        help=f"Skip series with fewer unique days (default: {MIN_HISTORY_DAYS})")
    parser.add_argument("--root",        default=".",
                        help="Repo root directory (default: cwd)")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level=get_settings().log_level,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )

    run_forecasts(
        root=Path(args.root),
        horizon_days=args.horizon,
        min_history_days=args.min_history,
    )


if __name__ == "__main__":
    _cli()
