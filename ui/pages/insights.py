"""Page 3 — AI Insights & Q&A: narrative summary + free-text question answering."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests
import streamlit as st

import api_client

# ── Header ────────────────────────────────────────────────────────────────────

st.title("🤖 AI Insights & Q&A")
st.caption(
    "Aggregated revenue figures are sent to DeepSeek for narrative generation. "
    "No raw transaction rows ever leave the system. "
    "Works without an API key — a deterministic fallback fires instead."
)

# ── Section 1: Revenue Insights ───────────────────────────────────────────────

st.subheader("📝 Generate Narrative Summary")
st.markdown(
    "Click the button to aggregate revenue by category and region, then have the AI "
    "produce a short business narrative."
)

if st.button("✨ Generate Summary", type="primary"):
    try:
        with st.spinner("Aggregating data and calling AI…"):
            result = api_client.post_insights()
        st.session_state["insights"] = result
    except requests.exceptions.ConnectionError:
        st.error("Cannot reach the API. Is the server running?")
    except requests.exceptions.HTTPError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text[:300]}")

if "insights" in st.session_state:
    ins = st.session_state["insights"]

    llm_badge = "🟢 LLM" if ins["llm_used"] else "🟡 Fallback (no API key)"
    st.markdown(f"*Source: {llm_badge}*")

    st.info(ins["summary"])

    # Revenue breakdowns from the response
    bc1, bc2 = st.columns(2)

    with bc1:
        st.markdown("**Revenue by Category**")
        df_cat = pd.DataFrame(ins["revenue_by_category"])
        if not df_cat.empty:
            df_cat["Revenue ($)"] = df_cat["revenue"].map("${:,.2f}".format)
            st.dataframe(
                df_cat[["category", "Revenue ($)"]].rename(columns={"category": "Category"}),
                use_container_width=True,
                hide_index=True,
            )

    with bc2:
        st.markdown("**Revenue by Region**")
        df_reg = pd.DataFrame(ins["revenue_by_region"])
        if not df_reg.empty:
            df_reg["Revenue ($)"] = df_reg["revenue"].map("${:,.2f}".format)
            st.dataframe(
                df_reg[["region", "Revenue ($)"]].rename(columns={"region": "Region"}),
                use_container_width=True,
                hide_index=True,
            )

st.divider()

# ── Section 2: Natural-Language Q&A ──────────────────────────────────────────

st.subheader("💬 Ask a Question About Your Data")
st.markdown(
    "Questions are answered using a **bounded, pre-aggregated context** built from the DB "
    "(revenue by category, region, and month; quality summary; forecast metadata). "
    "The AI is instructed to answer only from that context and say so if the answer isn't there."
)

example_questions = [
    "Which category has the highest revenue and by what percentage does it lead the second?",
    "How balanced is revenue across regions?",
    "What does the data quality look like and how many issues were repaired?",
    "What is the forecast horizon available in the database?",
]

with st.expander("💡 Example questions"):
    for q in example_questions:
        st.markdown(f"- *{q}*")

question = st.text_area(
    "Your question",
    placeholder="e.g. Which region has the strongest growth?",
    key="ask_input",
    height=80,
)

ask_col, _ = st.columns([1, 4])
ask_btn = ask_col.button("Ask →", type="primary", disabled=not question.strip())

if ask_btn and question.strip():
    try:
        with st.spinner("Building context and querying AI…"):
            result = api_client.post_ask(question.strip())
        st.session_state["ask_result"] = result
    except requests.exceptions.ConnectionError:
        st.error("Cannot reach the API.")
    except requests.exceptions.HTTPError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text[:300]}")

if "ask_result" in st.session_state:
    res = st.session_state["ask_result"]

    llm_badge = "🟢 LLM" if res["llm_used"] else "🟡 Fallback (no API key)"
    st.markdown(f"**Question:** *{res['question']}*")
    st.markdown(f"*Source: {llm_badge}*")

    st.success(res["answer"])

    with st.expander("🔍 Context preview (first 300 chars sent to AI)"):
        st.code(res["context_preview"], language=None)
