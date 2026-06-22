"""Page 3 — AI Insights & Q&A: narrative summary + free-text question answering."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import requests
import streamlit as st

import api_client

# ── Header ────────────────────────────────────────────────────────────────────

st.title("🤖 AI Insights & Q&A")
st.caption(
    "Revenue aggregates are sent to a local Ollama model for narrative generation. "
    "No raw transaction rows ever leave the system. "
    "If Ollama is still loading, a structured fallback response is returned instead."
)
st.divider()

# ── Section 1: Revenue Narrative ──────────────────────────────────────────────

st.subheader("📝 Revenue Summary")
st.markdown(
    "Generates a plain-English business narrative from category and region revenue totals. "
    "Use this to quickly understand what the data is saying without reading tables."
)

if st.button("✨ Generate Summary", type="primary"):
    try:
        with st.spinner("Aggregating data and generating narrative…"):
            result = api_client.post_insights()
        st.session_state["insights"] = result
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot reach the API. Is the server running?")
    except requests.exceptions.Timeout:
        st.error("❌ Request timed out — the AI may be loading. Try again in a moment.")
    except requests.exceptions.HTTPError as e:
        st.error(f"❌ API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        st.error(f"❌ {type(e).__name__}: {e}")

if "insights" in st.session_state:
    ins = st.session_state["insights"]

    llm_used = ins["llm_used"]
    badge_color = "#166534" if llm_used else "#92400E"
    badge_bg = "#DCFCE7" if llm_used else "#FEF3C7"
    badge_label = "🟢 Ollama (Local LLM)" if llm_used else "🟡 Deterministic Fallback — Ollama not yet ready"

    st.markdown(
        f"<span style='background:{badge_bg};color:{badge_color};"
        f"padding:3px 10px;border-radius:12px;font-size:0.82rem;font-weight:600;'>"
        f"{badge_label}</span>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    # Narrative in a styled card
    st.markdown(
        f"""
<div style='background:#F0F7FF;border-left:4px solid #2563EB;
            border-radius:6px;padding:16px 20px;margin-bottom:16px;
            font-size:0.97rem;line-height:1.7;color:#1E3A5F;'>
{ins["summary"]}
</div>
""",
        unsafe_allow_html=True,
    )

    # Revenue breakdown as charts
    st.markdown("#### Revenue Breakdown")
    bc1, bc2 = st.columns(2)

    with bc1:
        st.markdown("**By Category**")
        df_cat = pd.DataFrame(ins["revenue_by_category"])
        if not df_cat.empty:
            chart = (
                alt.Chart(df_cat)
                .mark_bar(cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
                .encode(
                    x=alt.X("revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
                    y=alt.Y("category:N", sort="-x", title=""),
                    color=alt.Color("category:N", legend=None, scale=alt.Scale(scheme="blues")),
                    tooltip=[
                        alt.Tooltip("category:N", title="Category"),
                        alt.Tooltip("revenue:Q", title="Revenue", format="$,.2f"),
                    ],
                )
                .properties(height=max(140, len(df_cat) * 36))
            )
            st.altair_chart(chart, use_container_width=True)

    with bc2:
        st.markdown("**By Region**")
        df_reg = pd.DataFrame(ins["revenue_by_region"])
        if not df_reg.empty:
            chart = (
                alt.Chart(df_reg)
                .mark_bar(cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
                .encode(
                    x=alt.X("revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
                    y=alt.Y("region:N", sort="-x", title=""),
                    color=alt.Color("region:N", legend=None, scale=alt.Scale(scheme="oranges")),
                    tooltip=[
                        alt.Tooltip("region:N", title="Region"),
                        alt.Tooltip("revenue:Q", title="Revenue", format="$,.2f"),
                    ],
                )
                .properties(height=max(140, len(df_reg) * 36))
            )
            st.altair_chart(chart, use_container_width=True)

st.divider()

# ── Section 2: Q&A ────────────────────────────────────────────────────────────

st.subheader("💬 Ask a Question About Your Data")
st.markdown(
    "Ask anything about your sales data in plain English. "
    "The AI answers using pre-aggregated revenue, quality, and forecast context — "
    "not raw rows — so responses are fast and privacy-safe."
)

example_questions = [
    "Which category has the highest revenue and by what percentage does it lead?",
    "How balanced is revenue across regions?",
    "What does the data quality look like — how many issues were caught?",
    "What is the forecast horizon available in the database?",
]

with st.expander("💡 Example questions to get started"):
    for q in example_questions:
        st.markdown(f"- *{q}*")

question = st.text_area(
    "Your question",
    placeholder="e.g. Which region has the strongest revenue growth?",
    key="ask_input",
    height=80,
)

ask_col, _ = st.columns([1, 5])
ask_btn = ask_col.button("Ask →", type="primary", disabled=not question.strip())

if ask_btn and question.strip():
    try:
        with st.spinner("Building context and querying AI…"):
            result = api_client.post_ask(question.strip())
        st.session_state["ask_result"] = result
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot reach the API. Is the server running?")
    except requests.exceptions.Timeout:
        st.error("❌ Request timed out — the AI may be loading. Try again.")
    except requests.exceptions.HTTPError as e:
        st.error(f"❌ API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        st.error(f"❌ {type(e).__name__}: {e}")

if "ask_result" in st.session_state:
    res = st.session_state["ask_result"]

    llm_used = res["llm_used"]
    badge_color = "#166534" if llm_used else "#92400E"
    badge_bg = "#DCFCE7" if llm_used else "#FEF3C7"
    badge_label = "🟢 Ollama (Local LLM)" if llm_used else "🟡 Deterministic Fallback"

    st.markdown("")

    # Question display
    st.markdown(
        f"<div style='background:#F8FAFC;border:1px solid #E2E8F0;"
        f"border-radius:6px;padding:12px 16px;margin-bottom:8px;"
        f"font-size:0.95rem;color:#334155;'>"
        f"<strong>Q:</strong> {res['question']}</div>",
        unsafe_allow_html=True,
    )

    # Answer card
    st.markdown(
        f"""
<div style='background:#F0FFF4;border-left:4px solid #16A34A;
            border-radius:6px;padding:16px 20px;margin-bottom:12px;
            font-size:0.97rem;line-height:1.7;color:#14532D;'>
<strong>Answer:</strong><br>{res["answer"]}
</div>
""",
        unsafe_allow_html=True,
    )

    st.markdown(
        f"<span style='background:{badge_bg};color:{badge_color};"
        f"padding:3px 10px;border-radius:12px;font-size:0.8rem;font-weight:600;'>"
        f"{badge_label}</span>",
        unsafe_allow_html=True,
    )

    with st.expander("🔍 Context sent to AI (first 300 chars)"):
        st.code(res["context_preview"], language=None)
