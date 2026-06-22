"""
src/ingestion/pipeline.py

Config-driven ingestion pipeline.

New 7-stage flow per sheet:
  1. READ     excel_io.read_workbook()
  2. ARCHIVE  copy original xlsx → data/archive/{YYYY-MM-DD}/
  3. RAW      write unmodified rows to raw.* Postgres table (all TEXT)
  4. CLEAN    clean_dataframe() — lowercase cols, null markers, date/numeric parsing
  5. DQ       run_dq_checks()  — DUPLICATE_ROW | PK_DUPLICATE | DATATYPE_VIOLATION
              └─ rejected rows → error.dq_rejected_rows (Postgres) + CSV report
  6. MAP      apply_sheet_config() — column rename + static columns
  7. WRITE    curated.* Postgres table  +  downstream CSV (for API / forecaster)

Usage
-----
    python -m src.ingestion.pipeline                        # all groups
    python -m src.ingestion.pipeline --mode historical      # reference + history only
    python -m src.ingestion.pipeline --mode incremental     # incremental batches only
    python -m src.ingestion.pipeline --root . --config config/ingestion.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from loguru import logger

from src.common.config import get_settings
from src.common.db import engine as _db_engine
from src.common.excel_io import read_workbook
from src.dq.checker import run_dq_checks
from src.ingestion.config_loader import (
    FileGroup,
    IngestionConfig,
    PipelineSettings,
    SheetConfig,
    load_config,
)
from src.ingestion.db_writer import (
    archive_file,
    write_curated,
    write_error,
    write_raw,
)

# ── Edge-case cleaning ────────────────────────────────────────────────────────


def _try_parse_dates(series: pd.Series, formats: list[str]) -> pd.Series:
    for fmt in formats:
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
        if parsed.notna().sum() >= series.notna().sum() * 0.5:
            return parsed
    return pd.to_datetime(series, errors="coerce")


def _try_numeric(series: pd.Series) -> pd.Series:
    stripped = series.astype(str).str.replace(r"[$,€£¥\s]", "", regex=True)
    numeric = pd.to_numeric(stripped, errors="coerce")
    if numeric.notna().sum() >= series.notna().sum() * 0.5:
        return numeric
    return series


_DATE_KEYWORDS = ("_date", "_ts", "_at", "_time", "datetime", "date", "timestamp")
_ID_KEYWORDS = (
    "_id",
    "_key",
    "order_id",
    "transaction_id",
    "campaign_id",
    "promo_id",
    "store_id",
    "location_id",
)


def clean_dataframe(df: pd.DataFrame, settings: PipelineSettings) -> pd.DataFrame:
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.replace(settings.null_values, pd.NA)
    df = df.dropna(how="all").reset_index(drop=True)
    for col in df.select_dtypes(include=["object", "string"]).columns:
        df[col] = df[col].apply(lambda x: x.strip() if isinstance(x, str) else x)
    for col in df.columns:
        if any(kw in col for kw in _DATE_KEYWORDS):
            df[col] = _try_parse_dates(df[col], settings.date_formats)
    for col in df.select_dtypes(include=["object", "string"]).columns:
        if not any(kw in col for kw in _ID_KEYWORDS):
            df[col] = _try_numeric(df[col])
    return df


# ── Column mapping + enrichment ───────────────────────────────────────────────


def apply_sheet_config(
    df: pd.DataFrame,
    sheet_cfg: SheetConfig,
    source_filename: str,
) -> pd.DataFrame:
    if sheet_cfg.column_map:
        cmap = {k.lower(): v for k, v in sheet_cfg.column_map.items()}
        matched = {c: cmap[c] for c in df.columns if c in cmap}
        missing = [k for k in cmap if k not in df.columns]
        if missing:
            logger.warning("    column_map: source columns not found → {}", missing)
        df = df[list(matched.keys())].rename(columns=matched)

    for col, val in sheet_cfg.add_columns.items():
        df[col] = source_filename if val is None else val

    if sheet_cfg.dedup_column and sheet_cfg.dedup_column in df.columns:
        before = len(df)
        df = df.drop_duplicates(subset=[sheet_cfg.dedup_column])
        dropped = before - len(df)
        if dropped:
            logger.info(
                "    dedup on '{}': removed {} duplicate(s)", sheet_cfg.dedup_column, dropped
            )

    return df


# ── CSV writer ────────────────────────────────────────────────────────────────


def write_csv(df: pd.DataFrame, dest: Path, write_mode: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if write_mode == "overwrite" or not dest.exists():
        df.to_csv(dest, index=False, encoding="utf-8")
        logger.info("    CSV  {} rows → {} (overwrite)", len(df), dest.name)
    else:
        existing_cols = pd.read_csv(dest, nrows=0).columns.tolist()
        df = df.reindex(columns=existing_cols, fill_value="")
        df.to_csv(dest, mode="a", header=False, index=False, encoding="utf-8")
        logger.info("    CSV  {} rows → {} (append)", len(df), dest.name)


# ── Per-file processing ───────────────────────────────────────────────────────


def _match_sheet_config(sheet_name: str, group: FileGroup) -> SheetConfig | None:
    for sc in group.sheets:
        if not sc.enabled:
            continue
        if sc.sheet == "*" or sc.sheet.lower() == sheet_name.lower():
            return sc
    return None


def process_file(
    fpath: Path,
    group: FileGroup,
    downstream_dir: Path,
    quality_dir: Path,
    archive_dir: Path,
    settings: PipelineSettings,
    load_batch_id: int | None = None,
) -> dict[str, int]:
    """
    Run the 7-stage pipeline for every matching sheet in *fpath*.

    Returns a summary dict: {inserted, raw_rows, error_rows}.
    """
    summary = {"inserted": 0, "raw_rows": 0, "error_rows": 0}
    logger.info("[{}] {}", group.name, fpath.name)

    # ── Stage 2: Archive original xlsx ───────────────────────────────────────
    try:
        archive_file(fpath, archive_dir)
    except Exception as exc:
        logger.warning("  Archive failed for {}: {}", fpath.name, exc)

    # ── Stage 1: Read ────────────────────────────────────────────────────────
    try:
        sheets_data = read_workbook(fpath)
    except Exception as exc:
        logger.error("  Failed to open {}: {}", fpath.name, exc)
        return summary

    if not sheets_data:
        logger.warning("  No readable sheets in {} — skipping", fpath.name)
        return summary

    # Use the module-level engine; ping to confirm DB is reachable
    from src.common.db import ping as _ping

    engine = _db_engine if _ping() else None
    if engine is None:
        logger.warning("  DB unreachable — raw/curated/error writes skipped; CSV write continues")

    for sheet_name, df_raw in sheets_data.items():
        sheet_cfg = _match_sheet_config(sheet_name, group)
        if sheet_cfg is None:
            logger.debug("  Sheet '{}' — no matching config, skipped", sheet_name)
            continue

        if df_raw.empty:
            logger.warning("  Sheet '{}' is empty — skipped", sheet_name)
            continue

        logger.info("  Sheet '{}': {} raw rows", sheet_name, len(df_raw))

        # ── Stage 3: RAW — write to raw.* before any transformation ─────────
        if engine and sheet_cfg.raw_table:
            # Normalise column names only (lowercase + strip) — no type coercion
            df_for_raw = df_raw.copy()
            df_for_raw.columns = [str(c).strip().lower() for c in df_for_raw.columns]
            n_raw = write_raw(
                df=df_for_raw,
                raw_table=sheet_cfg.raw_table,
                source_file=fpath.name,
                sheet_name=sheet_name,
                load_batch_id=load_batch_id,
                engine=engine,
            )
            summary["raw_rows"] += n_raw

        # ── Stage 4: Clean ───────────────────────────────────────────────────
        df = clean_dataframe(df_raw.copy(), settings)

        # ── Stage 5: DQ ─────────────────────────────────────────────────────
        df, rejected, dq_counts = run_dq_checks(
            df=df,
            pk_column=sheet_cfg.pk_column,
            datatype_rules=sheet_cfg.datatype_rules,
            source_file=fpath.name,
            sheet_name=sheet_name,
            quality_dir=quality_dir,
        )
        if dq_counts:
            logger.info("  Sheet '{}' DQ summary: {}", sheet_name, dq_counts)

        # Write rejected rows to error.dq_rejected_rows
        if engine and not rejected.empty:
            n_err = write_error(
                rejected=rejected,
                source_file=fpath.name,
                sheet_name=sheet_name,
                load_batch_id=load_batch_id,
                engine=engine,
            )
            summary["error_rows"] += n_err

        if df.empty:
            logger.warning("  Sheet '{}' empty after DQ checks — skipped", sheet_name)
            continue

        # ── Stage 6: Map ─────────────────────────────────────────────────────
        df = apply_sheet_config(df, sheet_cfg, fpath.name)

        if df.empty:
            logger.warning("  Sheet '{}' empty after column mapping — skipped", sheet_name)
            continue

        # ── Stage 7: Write — curated Postgres + downstream CSV ───────────────
        if engine and sheet_cfg.curated_table:
            # Resolve pk_column in post-map column names
            # (column_map may have renamed it; try mapped name first, then original)
            curated_pk = None
            if sheet_cfg.pk_column:
                mapped_pk = sheet_cfg.column_map.get(sheet_cfg.pk_column, sheet_cfg.pk_column)
                curated_pk = mapped_pk if mapped_pk in df.columns else sheet_cfg.pk_column

            n_cur = write_curated(
                df=df,
                curated_table=sheet_cfg.curated_table,
                write_mode=sheet_cfg.write_mode,
                pk_column=curated_pk,
                load_batch_id=load_batch_id,
                engine=engine,
            )
            summary["inserted"] += n_cur

        target = downstream_dir / sheet_cfg.target_csv
        write_csv(df, target, sheet_cfg.write_mode)

    return summary


# ── Pipeline orchestrator ─────────────────────────────────────────────────────


def run_pipeline(
    root: Path,
    config: IngestionConfig,
    mode: str = "all",
    load_batch_id: int | None = None,
) -> dict[str, int]:
    """
    Process all enabled file groups in declaration order.

    mode
        ``"all"``         — run every enabled group
        ``"historical"``  — only groups whose dir path contains "historical"
        ``"incremental"`` — only groups whose dir path contains "incremental"

    Returns aggregate summary {inserted, raw_rows, error_rows, files_processed}.
    """
    downstream_dir = root / config.settings.downstream_dir
    quality_dir = root / config.settings.quality_reports_dir
    archive_dir = root / config.settings.archive_dir
    downstream_dir.mkdir(parents=True, exist_ok=True)
    quality_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    totals: dict[str, int] = {"inserted": 0, "raw_rows": 0, "error_rows": 0, "files_processed": 0}

    for group in config.file_groups:
        if not group.enabled:
            logger.info("Skipping disabled group: {}", group.name)
            continue

        if mode != "all" and mode not in group.dir.lower():
            logger.debug("Skipping group '{}' — not in mode '{}'", group.name, mode)
            continue

        src_dir = root / group.dir
        if not src_dir.exists():
            logger.warning("Source dir not found: {} — skipping group '{}'", src_dir, group.name)
            continue

        files = sorted(src_dir.glob(group.file_pattern))
        if not files:
            logger.warning(
                "No files matching '{}' in {} — skipping group '{}'",
                group.file_pattern,
                src_dir,
                group.name,
            )
            continue

        for fpath in files:
            result = process_file(
                fpath,
                group,
                downstream_dir,
                quality_dir,
                archive_dir,
                config.settings,
                load_batch_id,
            )
            totals["inserted"] += result["inserted"]
            totals["raw_rows"] += result["raw_rows"]
            totals["error_rows"] += result["error_rows"]
            totals["files_processed"] += 1

    logger.info("Pipeline complete.")
    logger.info("  Files processed : {}", totals["files_processed"])
    logger.info("  Raw rows landed : {}", totals["raw_rows"])
    logger.info("  Error rows      : {}", totals["error_rows"])
    logger.info("  Curated inserted: {}", totals["inserted"])
    logger.info("  CSVs            → {}", downstream_dir)
    logger.info("  DQ reports      → {}", quality_dir)
    logger.info("  Archive         → {}", archive_dir)
    return totals


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="CPG Analytics ingestion pipeline — reads Excel, writes downstream CSVs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m src.ingestion.pipeline\n"
            "  python -m src.ingestion.pipeline --mode historical\n"
            "  python -m src.ingestion.pipeline --mode incremental\n"
            "  python -m src.ingestion.pipeline --config config/ingestion.json\n"
        ),
    )
    parser.add_argument("--root", default=".", help="Repo root (default: cwd)")
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to ingestion.json (default: <root>/config/ingestion.json or INGESTION_CONFIG env var)",
    )
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "historical", "incremental"],
        help=(
            "'all' runs every enabled file group (default); "
            "'historical' runs only groups whose dir contains 'historical'; "
            "'incremental' runs only groups whose dir contains 'incremental'"
        ),
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()

    settings = get_settings()
    cfg_path = Path(args.config) if args.config else root / settings.ingestion_config

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )

    config = load_config(cfg_path)
    logger.info(
        "Config loaded from {} — {} file group(s)  mode={}",
        cfg_path.name,
        len(config.file_groups),
        args.mode,
    )

    run_pipeline(root, config, mode=args.mode)


if __name__ == "__main__":
    _cli()
