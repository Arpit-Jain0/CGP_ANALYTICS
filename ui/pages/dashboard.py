"""Page 1 — Dashboard: revenue charts first, product performance, data reliability at bottom."""

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
st.caption("Revenue overview powered by the ingestion pipeline. Refresh after each new data load.")

# ── Date-range filter ─────────────────────────────────────────────────────────

with st.expander("🗓  Filter by date range", expanded=False):
    c1, c2 = st.columns(2)
    start_date = c1.date_input("From", value=None, key="dash_start")
    end_date = c2.date_input("To", value=None, key="dash_end")

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


@st.cache_data(ttl=60, show_spinner=False)
def fetch_products(start, end):
    return api_client.get_products(
        start_date=str(start) if start else None,
        end_date=str(end) if end else None,
        limit=20,
    )


try:
    with st.spinner("Loading dashboard…"):
        summary = fetch_summary(start_date, end_date)
        quality = fetch_quality()
        products = fetch_products(start_date, end_date)
except requests.exceptions.ConnectionError:
    st.error("❌ Cannot reach the API. Is the server running?  `uvicorn src.api.main:app --port 8000`")
    st.stop()
except requests.exceptions.Timeout:
    st.error("❌ Request timed out. Try refreshing.")
    st.stop()
except requests.exceptions.HTTPError as e:
    st.error(f"❌ API error: {e.response.status_code} — {e.response.text[:200]}")
    st.stop()
except Exception as e:
    st.error(f"❌ Unexpected error: {type(e).__name__}: {e}")
    st.stop()

# ── KPI metrics ───────────────────────────────────────────────────────────────

m1, m2, m3, m4 = st.columns(4)
m1.metric("💰 Total Revenue", f"${summary['total_revenue']:,.2f}")
m2.metric("🏆 Top Category", summary["top_category"])
m3.metric("📍 Top Region", summary["top_region"])
m4.metric("🧾 Transactions", f"{summary['transaction_count']:,}")

st.divider()

# ── Revenue charts ────────────────────────────────────────────────────────────

st.subheader("Revenue Breakdown")

left, right = st.columns(2)

with left:
    st.markdown("**By Category**")
    df_cat = pd.DataFrame(summary["revenue_by_category"])
    if not df_cat.empty:
        chart = (
            alt.Chart(df_cat)
            .mark_bar(cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
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
            .properties(height=240)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("No category data. Run ingestion first.")

with right:
    st.markdown("**By Region**")
    df_reg = pd.DataFrame(summary["revenue_by_region"])
    if not df_reg.empty:
        chart = (
            alt.Chart(df_reg)
            .mark_bar(cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
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
            .properties(height=240)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("No region data. Run ingestion first.")

st.divider()

# ── Product Performance ───────────────────────────────────────────────────────

st.subheader("🏷 Top Products by Revenue")

prod_data = products.get("products", []) if products else []

if prod_data:
    df_prod = pd.DataFrame(prod_data)

    chart_prod = (
        alt.Chart(df_prod)
        .mark_bar(cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
        .encode(
            x=alt.X("revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
            y=alt.Y("sku:N", sort="-x", title="SKU"),
            color=alt.Color(
                "category:N",
                legend=alt.Legend(title="Category"),
                scale=alt.Scale(scheme="tableau10"),
            ),
            tooltip=[
                alt.Tooltip("sku:N", title="SKU"),
                alt.Tooltip("brand:N", title="Brand"),
                alt.Tooltip("category:N", title="Category"),
                alt.Tooltip("revenue:Q", title="Revenue", format="$,.2f"),
                alt.Tooltip("transactions:Q", title="Transactions", format=","),
            ],
        )
        .properties(height=max(220, len(df_prod) * 22))
    )
    st.altair_chart(chart_prod, use_container_width=True)

    with st.expander("📋 Full product table"):
        df_display = df_prod.copy()
        df_display["revenue"] = df_display["revenue"].map("${:,.2f}".format)
        df_display["transactions"] = df_display["transactions"].map("{:,}".format)
        df_display.columns = [c.replace("_", " ").title() for c in df_display.columns]
        st.dataframe(df_display, use_container_width=True, hide_index=True)
else:
    st.info("No product data — run ingestion first.")

st.divider()

# ── Data Reliability ──────────────────────────────────────────────────────────

st.subheader("🔍 Data Reliability")
st.caption(
    "Summary of data quality issues caught by the pre-ingestion DQ gate across all pipeline runs."
)

q1, q2, q3 = st.columns(3)
q1.metric("Total Issues Logged", quality["total_issues"])
q2.metric("Total Load Batches", quality["total_batches"])
lb = quality.get("latest_batch")
q3.metric(
    "Latest Batch — Rows Inserted",
    lb["inserted"] if lb else "—",
    help="Rows successfully inserted in the most recent ingestion run.",
)

if quality["by_issue_type"]:
    rl, rr = st.columns(2)

    with rl:
        st.markdown("**Issues by Type**")
        df_issues = pd.DataFrame(quality["by_issue_type"])
        chart = (
            alt.Chart(df_issues)
            .mark_bar(cornerRadiusTopRight=5, cornerRadiusBottomRight=5)
            .encode(
                x=alt.X("count:Q", title="Count"),
                y=alt.Y("issue_type:N", sort="-x", title=""),
                color=alt.Color("issue_type:N", legend=None, scale=alt.Scale(scheme="reds")),
                tooltip=["issue_type:N", "count:Q"],
            )
            .properties(height=max(80, len(df_issues) * 36))
        )
        st.altair_chart(chart, use_container_width=True)

    with rr:
        st.markdown("**Issues by Action Taken**")
        df_actions = pd.DataFrame(quality["by_action_taken"])
        chart = (
            alt.Chart(df_actions)
            .mark_arc(innerRadius=40)
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
    st.markdown("**Latest Load Batch — Row Breakdown**")
    fields = ["inserted", "deduped", "rejected", "repaired", "flagged", "late_arriving"]
    batch_df = pd.DataFrame(
        [{"Metric": k.replace("_", " ").title(), "Count": lb.get(k, 0)} for k in fields]
    )
    st.dataframe(batch_df, use_container_width=True, hide_index=True)
