"""Page 2 — Forecast: Prophet predictions with period selector and forecast report."""

import sys
from datetime import date, timedelta
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
    "Select a category, region, and forecast period to see Prophet predictions "
    "with confidence bands. Click **Generate Forecast** to load the report."
)
st.divider()

# ── Controls ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _choices():
    try:
        s = api_client.get_summary()
        cats = [r["category"] for r in s["revenue_by_category"]]
        regs = [r["region"] for r in s["revenue_by_region"]]
        return cats, regs
    except Exception:
        return [], []


cats, regs = _choices()

ctrl1, ctrl2 = st.columns([2, 2])

with ctrl1:
    selected_cat = st.selectbox("Category", ["All categories"] + cats)

with ctrl2:
    selected_reg = st.selectbox("Region", ["All regions"] + regs)

# ── Period selector ───────────────────────────────────────────────────────────

st.markdown("#### Forecast Period")

PERIOD_OPTIONS = {
    "This Week (7 days)": 7,
    "This Month (30 days)": 30,
    "This Quarter (90 days)": 90,
    "Next 6 Months (180 days)": 180,
    "Custom": None,
}

period_col, custom_col = st.columns([2, 3])

with period_col:
    selected_period = st.selectbox("Select period", list(PERIOD_OPTIONS.keys()), index=2)

horizon = PERIOD_OPTIONS[selected_period]

if selected_period == "Custom":
    with custom_col:
        today = date.today()
        c1, c2 = st.columns(2)
        custom_start = c1.date_input("From", value=today, key="fc_start")
        custom_end = c2.date_input(
            "To", value=today + timedelta(days=90), key="fc_end", min_value=today
        )
        if custom_end > custom_start:
            horizon = (custom_end - custom_start).days
        else:
            st.warning("End date must be after start date.")
            horizon = 90
    period_label = f"{custom_start} → {custom_end} ({horizon} days)"
else:
    today = date.today()
    period_label = f"{today} → {today + timedelta(days=horizon)} ({horizon} days)"
    with custom_col:
        st.markdown(
            f"<div style='padding:10px 0 0 0; color:#555;'>📅 {period_label}</div>",
            unsafe_allow_html=True,
        )

st.markdown("")
run_btn = st.button("📊 Generate Forecast", type="primary", use_container_width=False)
st.divider()

# ── Fetch ─────────────────────────────────────────────────────────────────────

if run_btn:
    cat_arg = None if selected_cat == "All categories" else selected_cat
    reg_arg = None if selected_reg == "All regions" else selected_reg

    try:
        with st.spinner("Fetching forecast data…"):
            data = api_client.get_forecast(
                category=cat_arg,
                region=reg_arg,
                horizon=min(horizon, 90),  # model trained at 90-day horizon
            )
        st.session_state["forecast_data"] = data
        st.session_state["forecast_meta"] = {
            "cat": selected_cat,
            "reg": selected_reg,
            "period": selected_period,
            "period_label": period_label,
            "horizon": horizon,
        }
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot reach the API. Is the server running?")
        st.stop()
    except requests.exceptions.Timeout:
        st.error("❌ Request timed out fetching the forecast.")
        st.stop()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            st.warning(
                "No forecast data found. "
                "Run the forecaster first: `python -m src.forecasting.forecaster`"
            )
        else:
            st.error(f"❌ API error {e.response.status_code}: {e.response.text[:300]}")
        st.stop()
    except Exception as e:
        st.error(f"❌ Unexpected error: {type(e).__name__}: {e}")
        st.stop()

data = st.session_state.get("forecast_data")
meta = st.session_state.get("forecast_meta", {})

if not data or not data.get("points"):
    st.info("Select filters above and click **Generate Forecast** to view predictions.")
    st.stop()

# ── Build dataframe ───────────────────────────────────────────────────────────

df = pd.DataFrame(data["points"])
df["target_date"] = pd.to_datetime(df["target_date"])
df = df.sort_values("target_date").reset_index(drop=True)

points = data["points"]
revenues = df["predicted_revenue"].tolist()
lowers = df["yhat_lower"].tolist()
uppers = df["yhat_upper"].tolist()

total_rev = sum(revenues)
avg_daily = total_rev / len(revenues)
peak_idx = revenues.index(max(revenues))
peak_day = df.loc[peak_idx, "target_date"].strftime("%b %d, %Y")
peak_val = max(revenues)

# Growth: first-week avg vs last-week avg
first_week = revenues[:7] if len(revenues) >= 7 else revenues
last_week = revenues[-7:] if len(revenues) >= 7 else revenues
avg_start = sum(first_week) / len(first_week)
avg_end = sum(last_week) / len(last_week)
growth_pct = ((avg_end - avg_start) / avg_start * 100) if avg_start else 0

# Confidence: average band width as % of predicted revenue
band_widths = [u - l for u, l in zip(uppers, lowers)]
avg_band_pct = (sum(band_widths) / len(band_widths)) / avg_daily * 100 if avg_daily else 0

# ── Forecast header ───────────────────────────────────────────────────────────

st.markdown(
    f"### {meta.get('cat', 'All')} · {meta.get('reg', 'All')}  "
    f"<span style='font-size:0.85rem;color:#666;font-weight:normal;'>{meta.get('period_label','')}</span>",
    unsafe_allow_html=True,
)

# ── KPI strip ─────────────────────────────────────────────────────────────────

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Projected Revenue", f"${total_rev:,.0f}")
k2.metric("Avg Daily Revenue", f"${avg_daily:,.0f}")
k3.metric("Peak Day", peak_day, help=f"${peak_val:,.2f}")
k4.metric(
    "Revenue Trend",
    f"{'+' if growth_pct >= 0 else ''}{growth_pct:.1f}%",
    delta=f"{'Growing' if growth_pct >= 0 else 'Declining'}",
    delta_color="normal",
)
k5.metric(
    "Forecast Confidence",
    f"±{avg_band_pct:.0f}%",
    help="Average confidence band width as % of daily predicted revenue. Lower = more certain.",
)

st.divider()

# ── Chart ─────────────────────────────────────────────────────────────────────

st.markdown("#### Revenue Prediction Chart")

band = (
    alt.Chart(df)
    .mark_area(opacity=0.15, color="#2563EB")
    .encode(
        x=alt.X("target_date:T", title="Date", axis=alt.Axis(format="%b %d")),
        y=alt.Y("yhat_lower:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
        y2="yhat_upper:Q",
    )
)

line = (
    alt.Chart(df)
    .mark_line(color="#2563EB", strokeWidth=2.5)
    .encode(
        x="target_date:T",
        y="predicted_revenue:Q",
        tooltip=[
            alt.Tooltip("target_date:T", title="Date", format="%b %d, %Y"),
            alt.Tooltip("predicted_revenue:Q", title="Predicted Revenue", format="$,.2f"),
            alt.Tooltip("yhat_lower:Q", title="Lower Bound (90% CI)", format="$,.2f"),
            alt.Tooltip("yhat_upper:Q", title="Upper Bound (90% CI)", format="$,.2f"),
        ],
    )
)

chart = (band + line).properties(height=380).interactive()
st.altair_chart(chart, use_container_width=True)

st.caption(
    "Solid line = predicted revenue · Shaded area = 90% confidence interval · "
    "Band widens further out as uncertainty grows"
)

st.divider()

# ── Period Forecast Report ────────────────────────────────────────────────────

st.markdown("#### 📋 Period Forecast Report")
st.markdown(
    f"*What to expect for **{meta.get('cat', 'all categories')}** "
    f"in **{meta.get('reg', 'all regions')}** over the next **{meta.get('horizon', 90)} days***"
)

# Trend direction
if growth_pct > 5:
    trend_text = f"**Revenue is on an upward trend**, growing approximately **{growth_pct:.1f}%** from the start of the period to the end."
    trend_icon = "📈"
elif growth_pct < -5:
    trend_text = f"**Revenue is expected to decline** by approximately **{abs(growth_pct):.1f}%** over this period."
    trend_icon = "📉"
else:
    trend_text = f"**Revenue is expected to remain relatively stable**, with a minor {'increase' if growth_pct >= 0 else 'decrease'} of **{abs(growth_pct):.1f}%**."
    trend_icon = "➡️"

# Confidence interpretation
if avg_band_pct < 20:
    conf_text = "The model is **highly confident** in these predictions — the confidence band is tight."
elif avg_band_pct < 40:
    conf_text = "The model has **moderate confidence** — some variability expected but the trend direction is reliable."
else:
    conf_text = "The model shows **wider uncertainty** this far out — treat the trend direction as reliable, not the exact numbers."

# Weekly breakdown
df["week"] = df["target_date"].dt.to_period("W").astype(str)
weekly = df.groupby("week").agg(
    total=("predicted_revenue", "sum"),
    avg=("predicted_revenue", "mean"),
    peak=("predicted_revenue", "max"),
).reset_index()

r1, r2 = st.columns([3, 2])

with r1:
    st.markdown(
        f"""
{trend_icon} {trend_text}

- **Total projected revenue** for this period: **${total_rev:,.0f}**
- **Daily average**: ${avg_daily:,.0f} per day
- **Peak expected day**: {peak_day} at ${peak_val:,.0f}
- {conf_text}
        """
    )

with r2:
    st.markdown("**Weekly Revenue Breakdown**")
    if not weekly.empty:
        wk_chart = (
            alt.Chart(weekly)
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color="#2563EB")
            .encode(
                x=alt.X("total:Q", title="Total ($)", axis=alt.Axis(format="$,.0f")),
                y=alt.Y("week:N", sort=None, title=""),
                tooltip=[
                    alt.Tooltip("week:N", title="Week"),
                    alt.Tooltip("total:Q", title="Total Revenue", format="$,.2f"),
                    alt.Tooltip("avg:Q", title="Daily Avg", format="$,.2f"),
                    alt.Tooltip("peak:Q", title="Peak Day", format="$,.2f"),
                ],
            )
            .properties(height=max(120, len(weekly) * 28))
        )
        st.altair_chart(wk_chart, use_container_width=True)

st.divider()

# ── Raw table (collapsed) ─────────────────────────────────────────────────────

with st.expander("📋 Full daily forecast table"):
    df_display = df[["target_date", "predicted_revenue", "yhat_lower", "yhat_upper"]].copy()
    df_display["target_date"] = df_display["target_date"].dt.strftime("%Y-%m-%d")
    df_display.columns = ["Date", "Predicted ($)", "Lower Bound ($)", "Upper Bound ($)"]
    st.dataframe(
        df_display.style.format(
            {
                "Predicted ($)": "${:,.2f}",
                "Lower Bound ($)": "${:,.2f}",
                "Upper Bound ($)": "${:,.2f}",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )
