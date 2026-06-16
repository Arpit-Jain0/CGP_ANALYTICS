from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from src.api.models import BatchStats, IngestResponse
from src.api.queries import run_ingest

router = APIRouter()

_VALID_MODES = {"historical", "incremental"}


@router.post("/ingest", response_model=IngestResponse)
def ingest(
    mode: str = Query(
        default="incremental",
        description="'historical' runs all reference + history groups; "
                    "'incremental' runs the incremental batch group.",
    )
) -> IngestResponse:
    """
    Trigger the ingestion pipeline for the given mode.

    - **historical**: processes reference dimensions, promo windows, POS history,
      online history — reads from data/input/historical/.
    - **incremental**: processes any .xlsx files dropped into data/input/incremental/.

    Returns the load_batch audit row written to Postgres.
    """
    if mode not in _VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{mode}'. Choose from: {sorted(_VALID_MODES)}",
        )

    logger.info("POST /ingest  mode={}", mode)
    try:
        stats = run_ingest(mode)
    except Exception as exc:
        logger.exception("Ingest failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    batch = BatchStats(
        load_batch_id=stats.get("load_batch_id"),
        load_type=mode.upper(),
        inserted=stats["inserted"],
    )

    return IngestResponse(
        status="ok",
        mode=mode,
        files_processed=stats["files_processed"],
        batch=batch,
    )
