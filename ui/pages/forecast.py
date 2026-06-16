"""Page 2 — Forecast: Prophet predictions with confidence band."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import requests
import streamlit as st

import api_client

# ── Header ────────────────────────────────────────────────────────────────────

st.title("📈 Revenue Forecast")
st.caption(
    "Precomputed Prophet predictions from the database. "
    "Run `python -m src.forecasting.forecaster` to refresh."
)
st.divider()

# ── Controls ──────────────────────────────────────────────────────────────────

# Fetch summary once to populate dropdowns (no extra endpoint needed)
@st.cache_data(ttl=300, show_spinner=False)
def _choices():
    try:
        s = api_client.get_summary()
        cats = [r["category"] for r in s["revenue_by_category"]]
        regs = [r["region"]   for r in s["revenue_by_region"]]
        return cats, regs
    except Exception:
        return [], []

cats, regs = _choices()

ctrl1, ctrl2, ctrl3 = st.columns([2, 2, 2])

with ctrl1:
    cat_options = ["All categories"] + cats
    selected_cat = st.selectbox("Category", cat_options, index=0)

with ctrl2:
    reg_options = ["All regions"] + regs
    selected_reg = st.selectbox("Region", reg_options, index=0)

with ctrl3:
    horizon = st.slider("Horizon (days)", min_value=7, max_value=180, value=90, step=7)

run_btn = st.button("Generate Forecast", type="primary", use_container_width=False)

st.divider()

# ── Fetch & plot ──────────────────────────────────────────────────────────────

if run_btn or "forecast_data" not in st.session_state:
    cat_arg = None if selected_cat == "All categories" else selected_cat
    reg_arg = None if selected_reg == "All regions"    else selected_reg

    try:
        with st.spinner("Fetching forecast…"):
            data = api_client.get_forecast(
                category=cat_arg,
                region=reg_arg,
                horizon=horizon,
            )
        st.session_state["forecast_data"] = data
        st.session_state["forecast_label"] = (
            f"{selected_cat} · {selected_reg} · {horizon}-day horizon"
        )
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            st.warning(
                "No forecast data found. "
                "Run the forecaster first: `python -m src.forecasting.forecaster`"
            )
        else:
            st.error(f"API error {e.response.status_code}: {e.response.text[:300]}")
        st.stop()
    except requests.exceptions.ConnectionError:
        st.error("Cannot reach the API.")
        st.stop()

data  = st.session_state.get("forecast_data")
label = st.session_state.get("forecast_label", "")

if not data or not data.get("points"):
    st.info("Select filters above and click **Generate Forecast**.")
    st.stop()

# ── Summary strip ─────────────────────────────────────────────────────────────

p1, p2, p3, p4 = st.columns(4)
points = data["points"]
revenues = [p["predicted_revenue"] for p in points]
p1.metric("Run Date",      str(data.get("run_date", "—")))
p2.metric("Model",         data.get("model_version", "—"))
p3.metric("Peak Forecast", f"${max(revenues):,.2f}")
p4.metric("Avg Forecast",  f"${sum(revenues)/len(revenues):,.2f}")

st.markdown(f"*{label}*  ·  {len(points)} data points")

# ── Chart ─────────────────────────────────────────────────────────────────────

df = pd.DataFrame(points)
df["target_date"] = pd.to_datetime(df["target_date"])

band = (
    alt.Chart(df)
    .mark_area(opacity=0.20, color="#2563EB")
    .encode(
        x=alt.X("target_date:T", title="Date"),
        y=alt.Y("yhat_lower:Q",  title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
        y2="yhat_upper:Q",
    )
)

line = (
    alt.Chart(df)
    .mark_line(color="#2563EB", strokeWidth=2)
    .encode(
        x=alt.X("target_date:T"),
        y=alt.Y("predicted_revenue:Q"),
        tooltip=[
            alt.Tooltip("target_date:T",        title="Date"),
            alt.Tooltip("predicted_revenue:Q",  title="Predicted",   format="$,.2f"),
            alt.Tooltip("yhat_lower:Q",         title="Lower bound", format="$,.2f"),
            alt.Tooltip("yhat_upper:Q",         title="Upper bound", format="$,.2f"),
        ],
    )
)

points_layer = (
    alt.Chart(df)
    .mark_point(color="#2563EB", size=30, opacity=0.6)
    .encode(
        x="target_date:T",
        y="predicted_revenue:Q",
        tooltip=[
            alt.Tooltip("target_date:T",       title="Date"),
            alt.Tooltip("predicted_revenue:Q", title="Predicted", format="$,.2f"),
        ],
    )
)

chart = (band + line + points_layer).properties(height=420).interactive()
st.altair_chart(chart, use_container_width=True)

# ── Raw table (collapsed) ─────────────────────────────────────────────────────

with st.expander("📋 Raw forecast table"):
    df_display = df.rename(columns={
        "target_date":       "Date",
        "predicted_revenue": "Predicted ($)",
        "yhat_lower":        "Lower bound ($)",
        "yhat_upper":        "Upper bound ($)",
    })
    st.dataframe(
        df_display.style.format({
            "Predicted ($)":   "${:,.2f}",
            "Lower bound ($)": "${:,.2f}",
            "Upper bound ($)": "${:,.2f}",
        }),
        use_container_width=True,
        hide_index=True,
    )
