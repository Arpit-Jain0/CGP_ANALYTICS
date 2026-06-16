"""Page 1 — Dashboard: revenue KPIs + data-reliability panel."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import requests
import streamlit as st

import api_client

# ── Header ────────────────────────────────────────────────────────────────────

st.title("📊 Dashboard")
st.caption("Revenue overview and data-quality snapshot. Data is sourced from the ingestion pipeline via the API.")

# ── Date-range filter ─────────────────────────────────────────────────────────

with st.expander("🗓  Filter by date range", expanded=False):
    c1, c2 = st.columns(2)
    start_date = c1.date_input("From", value=None, key="dash_start")
    end_date   = c2.date_input("To",   value=None, key="dash_end")

st.divider()

# ── Fetch data ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_summary(start, end):
    return api_client.get_summary(
        start_date=str(start) if start else None,
        end_date=str(end) if end else None,
    )

@st.cache_data(ttl=60, show_spinner=False)
def fetch_quality():
    return api_client.get_quality()

try:
    with st.spinner("Loading summary…"):
        summary = fetch_summary(start_date, end_date)
    with st.spinner("Loading quality stats…"):
        quality = fetch_quality()
except requests.exceptions.ConnectionError:
    st.error("Cannot reach the API. Is the server running?  `uvicorn src.api.main:app --port 8000`")
    st.stop()
except requests.exceptions.HTTPError as e:
    st.error(f"API error: {e.response.status_code} — {e.response.text[:200]}")
    st.stop()

# ── KPI metrics row ───────────────────────────────────────────────────────────

m1, m2, m3, m4 = st.columns(4)
m1.metric("💰 Total Revenue",    f"${summary['total_revenue']:,.2f}")
m2.metric("🏆 Top Category",     summary["top_category"])
m3.metric("📍 Top Region",       summary["top_region"])
m4.metric("🧾 Transactions",     f"{summary['transaction_count']:,}")

st.divider()

# ── Revenue charts ────────────────────────────────────────────────────────────

left, right = st.columns(2)

with left:
    st.subheader("Revenue by Category")
    df_cat = pd.DataFrame(summary["revenue_by_category"])
    if not df_cat.empty:
        chart = (
            alt.Chart(df_cat)
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
            .encode(
                x=alt.X("revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
                y=alt.Y("category:N", sort="-x", title=""),
                color=alt.Color(
                    "category:N",
                    legend=None,
                    scale=alt.Scale(scheme="blues"),
                ),
                tooltip=[
                    alt.Tooltip("category:N", title="Category"),
                    alt.Tooltip("revenue:Q", title="Revenue", format="$,.2f"),
                ],
            )
            .properties(height=220)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("No category data.")

with right:
    st.subheader("Revenue by Region")
    df_reg = pd.DataFrame(summary["revenue_by_region"])
    if not df_reg.empty:
        chart = (
            alt.Chart(df_reg)
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
            .encode(
                x=alt.X("revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
                y=alt.Y("region:N", sort="-x", title=""),
                color=alt.Color(
                    "region:N",
                    legend=None,
                    scale=alt.Scale(scheme="oranges"),
                ),
                tooltip=[
                    alt.Tooltip("region:N", title="Region"),
                    alt.Tooltip("revenue:Q", title="Revenue", format="$,.2f"),
                ],
            )
            .properties(height=180)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("No region data.")

st.divider()

# ── Data Reliability panel ────────────────────────────────────────────────────

st.subheader("🔍 Data Reliability")

q1, q2, q3 = st.columns(3)
q1.metric("Total Issues Logged",   quality["total_issues"])
q2.metric("Total Load Batches",    quality["total_batches"])
lb = quality.get("latest_batch")
q3.metric(
    "Latest Batch (inserted)",
    lb["inserted"] if lb else "—",
    help="Rows inserted in the most recent ingest run",
)

if quality["by_issue_type"]:
    rl, rr = st.columns(2)

    with rl:
        st.markdown("**Issues by Type**")
        df_issues = pd.DataFrame(quality["by_issue_type"])
        chart = (
            alt.Chart(df_issues)
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
            .encode(
                x=alt.X("count:Q", title="Count"),
                y=alt.Y("issue_type:N", sort="-x", title=""),
                color=alt.Color(
                    "issue_type:N",
                    legend=None,
                    scale=alt.Scale(scheme="reds"),
                ),
                tooltip=["issue_type:N", "count:Q"],
            )
            .properties(height=max(80, len(df_issues) * 32))
        )
        st.altair_chart(chart, use_container_width=True)

    with rr:
        st.markdown("**Issues by Action Taken**")
        df_actions = pd.DataFrame(quality["by_action_taken"])
        chart = (
            alt.Chart(df_actions)
            .mark_arc()
            .encode(
                theta=alt.Theta("count:Q"),
                color=alt.Color(
                    "action_taken:N",
                    scale=alt.Scale(scheme="tableau10"),
                    legend=alt.Legend(title="Action"),
                ),
                tooltip=["action_taken:N", "count:Q"],
            )
            .properties(height=200)
        )
        st.altair_chart(chart, use_container_width=True)
else:
    st.info("No quality issues logged yet. Run an ingest to populate.")

if lb:
    st.markdown("**Latest Load Batch**")
    fields = ["inserted", "deduped", "rejected", "repaired", "flagged", "late_arriving"]
    batch_df = pd.DataFrame(
        [{"metric": k.replace("_", " ").title(), "value": lb.get(k, 0)} for k in fields]
    )
    st.dataframe(batch_df, use_container_width=True, hide_index=True)
