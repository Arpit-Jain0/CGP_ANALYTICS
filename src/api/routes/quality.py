from fastapi import APIRouter, HTTPException
from loguru import logger

from src.api.models import BatchStats, QualityActionCount, QualityIssueCount, QualityResponse
from src.api.queries import get_quality_summary

router = APIRouter()


@router.get("/quality", response_model=QualityResponse)
def quality() -> QualityResponse:
    """
    Data-quality summary from the Postgres audit tables.

    Returns issue counts by type and action, plus the latest load_batch stats.
    Tables are populated by the ingestion pipeline (via POST /ingest).
    """
    logger.info("GET /quality")
    try:
        qs = get_quality_summary()
    except Exception as exc:
        logger.exception("Quality query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    latest = None
    if qs["latest_batch"]:
        b = qs["latest_batch"]
        latest = BatchStats(
            load_batch_id=b.get("load_batch_id"),
            load_type=b.get("load_type", "UNKNOWN"),
            source_file=b.get("source_file"),
            rows_in=int(b.get("rows_in") or 0),
            inserted=int(b.get("inserted") or 0),
            deduped=int(b.get("deduped") or 0),
            rejected=int(b.get("rejected") or 0),
            repaired=int(b.get("repaired") or 0),
            flagged=int(b.get("flagged") or 0),
            late_arriving=int(b.get("late_arriving") or 0),
        )

    return QualityResponse(
        total_issues=qs["total_issues"],
        by_issue_type=[QualityIssueCount(**r) for r in qs["by_issue_type"]],
        by_action_taken=[QualityActionCount(**r) for r in qs["by_action_taken"]],
        latest_batch=latest,
        total_batches=qs["total_batches"],
    )
