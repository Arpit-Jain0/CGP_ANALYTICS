"""
src/api/llm.py

Deterministic response generators for /insights and /ask.
LLM integration removed — all responses are computed from the aggregated data.
"""
from __future__ import annotations


# ── /insights ─────────────────────────────────────────────────────────────────

def _insights_summary(agg: dict) -> str:
    total = agg["total_revenue"]
    lines = [
        f"Total revenue: ${total:,.2f}",
        "",
        "By Category:",
        *[f"  {r['category']}: ${r['revenue']:,.2f}  ({r['revenue']/total:.1%})"
          for r in agg["by_category"]],
        "",
        "By Region:",
        *[f"  {r['region']}: ${r['revenue']:,.2f}  ({r['revenue']/total:.1%})"
          for r in agg["by_region"]],
    ]
    if agg["by_category"]:
        top = agg["by_category"][0]
        lines.append(
            f"\nLeading category '{top['category']}' accounts for "
            f"{top['revenue']/total:.1%} of total revenue."
        )
    return "\n".join(lines)


async def generate_insights(agg: dict) -> tuple[str, bool]:
    """Returns (summary_text, llm_used=False)."""
    return _insights_summary(agg), False


# ── /ask ──────────────────────────────────────────────────────────────────────

async def answer_question(context: str, question: str) -> tuple[str, bool]:
    """Returns the bounded context as the answer with llm_used=False."""
    answer = (
        f"Here are the relevant figures from your data:\n\n"
        f"{context}\n\n"
        f"Question: {question}"
    )
    return answer, False
