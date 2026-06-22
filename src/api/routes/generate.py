"""
src/api/routes/generate.py

POST /generate-batch?type=pos|online

Generates a new incremental batch xlsx for the current ISO week.
If the file already exists for this week, returns status="exists" immediately.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from src.api.models import GenerateBatchResponse

# scripts/ is not a package — add it to sys.path once at import time
_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    from generate_data import generate_weekly_batch as _gen_batch
except ImportError as _e:
    _gen_batch = None  # type: ignore[assignment]
    _IMPORT_ERR = str(_e)
else:
    _IMPORT_ERR = ""

_ROOT = Path(__file__).resolve().parents[3]  # project root

router = APIRouter()


@router.post("/generate-batch", response_model=GenerateBatchResponse)
def generate_batch(
    type: str = Query(..., pattern="^(pos|online)$", description="pos or online"),
) -> GenerateBatchResponse:
    """
    Generate a new incremental batch file for the current ISO week.

    - **type=pos**    → writes `{YYYY-MM-DD}_pos.xlsx` with a `Sales` sheet
    - **type=online** → writes `{YYYY-MM-DD}_online.xlsx` with an `Orders` sheet

    Any existing `*_pos.xlsx` or `*_online.xlsx` files in the incremental
    folder are deleted first so only the latest batch is kept.
    """
    if _gen_batch is None:
        raise HTTPException(
            status_code=500,
            detail=f"Could not import generate_data module: {_IMPORT_ERR}",
        )
    try:
        result = _gen_batch(batch_type=type, root=_ROOT)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return GenerateBatchResponse(**result)
