from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from src.api.models import ForecastPoint, ForecastResponse
from src.api.queries import get_forecast_rows

router = APIRouter()


@router.get("/forecast", response_model=ForecastResponse)
def forecast(
    category: str | None = Query(default=None, description="Filter by product category"),
    region: str | None = Query(default=None, description="Filter by region (MIDWEST, NORTHEAST, …)"),
    horizon: int = Query(default=90, ge=1, le=365, description="Max forecast days to return"),
) -> ForecastResponse:
    """
    Precomputed Prophet forecast from the **forecast_results** table.

    Rows are produced by running the forecaster:
    `python -m src.forecasting.forecaster`

    Returns the latest run_date's predictions, optionally filtered by
    **category** and/or **region**.
    """
    logger.info("GET /forecast  category={} region={} horizon={}", category, region, horizon)
    try:
        data = get_forecast_rows(category=category, region=region, horizon=horizon)
    except Exception as exc:
        logger.exception("Forecast query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not data["points"]:
        raise HTTPException(
            status_code=404,
            detail=(
                "No forecast rows found. "
                "Run: python -m src.forecasting.forecaster"
            ),
        )

    return ForecastResponse(
        category=category,
        region=region,
        horizon=len(data["points"]),
        run_date=data["run_date"],
        model_version=data["model_version"],
        points=[ForecastPoint(**p) for p in data["points"]],
    )
