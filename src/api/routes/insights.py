import asyncio

from fastapi import APIRouter, HTTPException
from loguru import logger

from src.api.llm import generate_insights
from src.api.models import CategoryRevenue, InsightsResponse, RegionRevenue
from src.api.queries import get_insights_aggregates

router = APIRouter()


@router.post("/insights", response_model=InsightsResponse)
async def insights() -> InsightsResponse:
    """
    Aggregate revenue by region + category from ingested data, then ask the
    LLM to produce a short natural-language narrative.

    Only aggregate numbers are sent to the LLM — no raw transaction rows.

    When **DEEPSEEK_API_KEY** is not set, returns a deterministic template
    summary instead (still fully functional).
    """
    logger.info("POST /insights")
    try:
        agg = await asyncio.get_event_loop().run_in_executor(None, get_insights_aggregates)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Downstream data not found — run ingestion first. ({exc})",
        ) from exc
    except Exception as exc:
        logger.exception("Insights aggregation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        summary_text, llm_used = await generate_insights(agg)
    except Exception as exc:
        logger.exception("LLM call failed")
        raise HTTPException(status_code=500, detail=f"LLM error: {exc}") from exc

    return InsightsResponse(
        summary=summary_text,
        llm_used=llm_used,
        revenue_by_category=[CategoryRevenue(**r) for r in agg["by_category"]],
        revenue_by_region=[RegionRevenue(**r) for r in agg["by_region"]],
    )
