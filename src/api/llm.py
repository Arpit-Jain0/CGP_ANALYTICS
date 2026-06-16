"""
src/api/llm.py

DeepSeek chat-completions client with deterministic fallback.

Rules:
  - Never send raw transaction rows — aggregates/context only.
  - When DEEPSEEK_API_KEY is absent or blank, the fallback fires automatically.
  - Fallback responses are clearly labeled so the caller knows no LLM was used.
"""
from __future__ import annotations

import httpx
from loguru import logger

from src.common.config import get_settings

_TIMEOUT = 30.0     # seconds
_MAX_TOKENS = 600


# ── Raw API call ──────────────────────────────────────────────────────────────

async def _call_deepseek(messages: list[dict]) -> str | None:
    """
    POST to DeepSeek chat completions.
    Returns the assistant content string, or None on any error.
    """
    settings = get_settings()
    if not settings.llm_enabled:
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model,
                    "messages": messages,
                    "max_tokens": _MAX_TOKENS,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except httpx.HTTPStatusError as exc:
        logger.warning("DeepSeek HTTP error {}: {}", exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        logger.warning("DeepSeek call failed: {}", exc)
    return None


# ── /insights ─────────────────────────────────────────────────────────────────

def _insights_fallback(agg: dict) -> str:
    """Deterministic template summary — used when LLM is unavailable."""
    lines = [
        "Revenue Summary (computed from ingested data — LLM unavailable):",
        f"  Total revenue: ${agg['total_revenue']:,.2f}",
        "",
        "By Category:",
        *[f"  {r['category']}: ${r['revenue']:,.2f}" for r in agg["by_category"]],
        "",
        "By Region:",
        *[f"  {r['region']}: ${r['revenue']:,.2f}" for r in agg["by_region"]],
    ]
    if agg["by_category"]:
        top_cat = agg["by_category"][0]
        share = top_cat["revenue"] / agg["total_revenue"] * 100
        lines.append(
            f"\nLeading category '{top_cat['category']}' accounts for {share:.1f}% of total revenue."
        )
    return "\n".join(lines)


def _insights_prompt(agg: dict) -> list[dict]:
    figures = (
        f"Total revenue: ${agg['total_revenue']:,.2f}\n"
        + "Category breakdown:\n"
        + "\n".join(f"  {r['category']}: ${r['revenue']:,.2f}" for r in agg["by_category"])
        + "\nRegion breakdown:\n"
        + "\n".join(f"  {r['region']}: ${r['revenue']:,.2f}" for r in agg["by_region"])
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a concise CPG analytics assistant. "
                "Summarize the provided revenue figures in 3–5 sentences. "
                "Use ONLY the numbers given — do not invent or extrapolate data. "
                "Highlight the top performer(s) and any notable patterns."
            ),
        },
        {
            "role": "user",
            "content": f"Here are the aggregated revenue figures:\n\n{figures}\n\nWrite a brief narrative summary.",
        },
    ]


async def generate_insights(agg: dict) -> tuple[str, bool]:
    """
    Returns (summary_text, llm_used).
    Falls back to deterministic template when LLM is unavailable.
    """
    result = await _call_deepseek(_insights_prompt(agg))
    if result:
        return result, True
    return _insights_fallback(agg), False


# ── /ask ──────────────────────────────────────────────────────────────────────

def _ask_fallback(context: str, question: str) -> str:
    return (
        "LLM unavailable — here are the relevant data figures to answer your question:\n\n"
        + context
        + f"\n\nQuestion asked: {question}\n"
        "(Connect a DEEPSEEK_API_KEY to get a natural-language answer.)"
    )


def _ask_prompt(context: str, question: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are a CPG analytics assistant. "
                "Answer the user's question using ONLY the data context provided below. "
                "If the answer is not present in the context, say so explicitly — "
                "do not guess or use outside knowledge. "
                "Keep answers concise (2–4 sentences max)."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Data context:\n{context}\n\n"
                f"Question: {question}"
            ),
        },
    ]


async def answer_question(context: str, question: str) -> tuple[str, bool]:
    """
    Returns (answer_text, llm_used).
    Falls back to context dump when LLM is unavailable.
    """
    result = await _call_deepseek(_ask_prompt(context, question))
    if result:
        return result, True
    return _ask_fallback(context, question), False
