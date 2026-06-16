from fastapi import APIRouter
from src.api.models import HealthResponse
from src.common.db import ping

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness + DB connectivity check."""
    connected = ping()
    return HealthResponse(
        status="ok" if connected else "degraded",
        db_connected=connected,
    )
