from datetime import date

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from src.api.models import CategoryRevenue, RegionRevenue, SummaryResponse
from src.api.queries import get_revenue_kpis

router = APIRouter()


@router.get("/summary", response_model=SummaryResponse)
def summary(
    start_date: date | None = Query(default=None, description="Filter from this date (inclusive)"),
    end_date: date | None = Query(default=None, description="Filter to this date (inclusive)"),
) -> SummaryResponse:
    """
    Revenue KPIs aggregated from ingested sales data.

    Returns total revenue, top category/region, and full breakdowns.
    Optionally scoped to a date range via **start_date** / **end_date**.
    """
    logger.info("GET /summary  start={} end={}", start_date, end_date)
    try:
        kpis = get_revenue_kpis(start_date=start_date, end_date=end_date)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Downstream data not found — run ingestion first. ({exc})",
        ) from exc
    except Exception as exc:
        logger.exception("Summary query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return SummaryResponse(
        total_revenue=kpis["total_revenue"],
        top_category=kpis["top_category"],
        top_region=kpis["top_region"],
        transaction_count=kpis["transaction_count"],
        revenue_by_category=[CategoryRevenue(**r) for r in kpis["by_category"]],
        revenue_by_region=[RegionRevenue(**r) for r in kpis["by_region"]],
        start_date=kpis["start_date"],
        end_date=kpis["end_date"],
    )
