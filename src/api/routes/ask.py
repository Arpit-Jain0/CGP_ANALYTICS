import asyncio

from fastapi import APIRouter, HTTPException
from loguru import logger

from src.api.llm import answer_question
from src.api.models import AskRequest, AskResponse
from src.api.queries import build_bounded_context

router = APIRouter()


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest) -> AskResponse:
    """
    Natural-language Q&A over the CPG data.

    Builds a **compact, bounded context** from pre-aggregated DB tables (revenue
    by category, by region, by month; quality summary; forecast metadata) and
    sends it + the question to the LLM.

    Safety guarantees:
    - No raw transaction rows are ever sent to the LLM.
    - The LLM is instructed to answer only from the provided context.
    - No user-supplied SQL is executed.

    When **DEEPSEEK_API_KEY** is absent the endpoint still responds with the
    bounded context rendered as plain text so the caller can see the data.
    """
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    logger.info("POST /ask  question={!r}", body.question[:80])

    try:
        context = await asyncio.get_event_loop().run_in_executor(None, build_bounded_context)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Downstream data not found — run ingestion first. ({exc})",
        ) from exc
    except Exception as exc:
        logger.exception("Context build failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        answer, llm_used = await answer_question(context, body.question)
    except Exception as exc:
        logger.exception("LLM call failed")
        raise HTTPException(status_code=500, detail=f"LLM error: {exc}") from exc

    return AskResponse(
        question=body.question,
        answer=answer,
        llm_used=llm_used,
        context_preview=context[:300],
    )
