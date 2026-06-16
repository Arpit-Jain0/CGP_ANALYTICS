"""Pydantic response models for all API endpoints."""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str          # "ok" | "degraded"
    db_connected: bool
    version: str = "1.0.0"


# ── Ingest ────────────────────────────────────────────────────────────────────

class BatchStats(BaseModel):
    load_batch_id: int | None = None
    load_type: str
    source_file: str | None = None
    rows_in: int = 0
    inserted: int = 0
    deduped: int = 0
    rejected: int = 0
    repaired: int = 0
    flagged: int = 0
    late_arriving: int = 0


class IngestResponse(BaseModel):
    status: str                  # "ok" | "error"
    mode: str
    files_processed: int
    batch: BatchStats


# ── Summary ───────────────────────────────────────────────────────────────────

class CategoryRevenue(BaseModel):
    category: str
    revenue: float


class RegionRevenue(BaseModel):
    region: str
    revenue: float


class SummaryResponse(BaseModel):
    total_revenue: float
    top_category: str
    top_region: str
    revenue_by_category: list[CategoryRevenue]
    revenue_by_region: list[RegionRevenue]
    transaction_count: int
    start_date: date | None = None
    end_date: date | None = None


# ── Quality ───────────────────────────────────────────────────────────────────

class QualityIssueCount(BaseModel):
    issue_type: str
    count: int


class QualityActionCount(BaseModel):
    action_taken: str
    count: int


class QualityResponse(BaseModel):
    total_issues: int
    by_issue_type: list[QualityIssueCount]
    by_action_taken: list[QualityActionCount]
    latest_batch: BatchStats | None = None
    total_batches: int = 0


# ── Forecast ──────────────────────────────────────────────────────────────────

class ForecastPoint(BaseModel):
    target_date: date
    predicted_revenue: float
    yhat_lower: float | None = None
    yhat_upper: float | None = None


class ForecastResponse(BaseModel):
    category: str | None = None
    region: str | None = None
    horizon: int
    run_date: date | None = None
    model_version: str | None = None
    points: list[ForecastPoint]


# ── AI / LLM ─────────────────────────────────────────────────────────────────

class InsightsResponse(BaseModel):
    summary: str
    llm_used: bool
    revenue_by_category: list[CategoryRevenue]
    revenue_by_region: list[RegionRevenue]


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    question: str
    answer: str
    llm_used: bool
    context_preview: str    # first 300 chars of context, for transparency
