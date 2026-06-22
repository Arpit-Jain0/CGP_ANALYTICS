"""Page 4 — Data Loads: trigger historical / incremental ingestion and inspect audit results."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests
import streamlit as st

import api_client

# ── Header ────────────────────────────────────────────────────────────────────

st.title("🔄 Data Loads")
st.caption(
    "Trigger the ingestion pipeline from the UI. "
    "Each run writes a **load_batch** record to Postgres — visible in the audit table below."
)
st.divider()

# ── Explanation cards ─────────────────────────────────────────────────────────

with st.container(border=True):
    h_col, i_col = st.columns(2)

    with h_col:
        st.markdown("#### 📚 Historical Load")
        st.markdown(
            "Processes **reference dimensions** (products, stores, regions, calendar), "
            "promo windows, marketing campaigns, competitor prices, and the full "
            "POS + Online transaction history.\n\n"
            "Run this **once** to prime the system.  "
            "Re-running overwrites dimension CSVs and appends history (dedup applies)."
        )
        hist_btn = st.button(
            "▶ Run Historical Load",
            key="hist_btn",
            type="primary",
            use_container_width=True,
        )

    with i_col:
        st.markdown("#### ⚡ Incremental Load")
        st.markdown(
            "Processes any `.xlsx` files placed in `data/input/incremental/`. "
            "Designed to be run daily or on demand.\n\n"
            "**Re-running is safe:** the pipeline appends new rows and deduplicates on "
            "`transaction_id`, so repeat incremental runs converge to the same state "
            "— no double-counting."
        )
        incr_btn = st.button(
            "▶ Run Incremental Load",
            key="incr_btn",
            type="primary",
            use_container_width=True,
        )

st.divider()

# ── Run handlers ──────────────────────────────────────────────────────────────


def _run_ingest(mode: str):
    try:
        with st.spinner(f"Running {mode} ingest — this may take 20-60 seconds…"):
            result = api_client.post_ingest(mode)
        st.session_state[f"ingest_{mode}"] = result
        st.session_state["latest_mode"] = mode
    except requests.exceptions.ConnectionError:
        st.error(
            "❌ Cannot reach the API. Make sure the server is running: `uvicorn src.api.main:app --reload --port 8000`"
        )
    except requests.exceptions.Timeout:
        st.error(
            "❌ Request timed out. The pipeline may still be running — check the API terminal output."
        )
    except requests.exceptions.HTTPError as e:
        st.error(f"❌ API error {e.response.status_code}: {e.response.text[:600]}")
    except Exception as e:
        st.error(f"❌ Unexpected error: {type(e).__name__}: {e}")


if hist_btn:
    _run_ingest("historical")

if incr_btn:
    _run_ingest("incremental")

# ── Audit result ──────────────────────────────────────────────────────────────

latest_mode = st.session_state.get("latest_mode")
if latest_mode:
    result = st.session_state.get(f"ingest_{latest_mode}")
    if result:
        st.subheader(f"✅ {latest_mode.title()} Load — Result")

        r1, r2, r3 = st.columns(3)
        r1.metric("Status", result["status"].upper())
        r2.metric("Files Processed", result["files_processed"])
        r3.metric("Load Batch ID", result["batch"].get("load_batch_id") or "—")

        batch = result["batch"]
        audit_fields = [
            ("rows_in", "Rows In (source)"),
            ("inserted", "Inserted"),
            ("deduped", "Deduplicated"),
            ("rejected", "Rejected"),
            ("repaired", "Repaired"),
            ("flagged", "Flagged"),
            ("late_arriving", "Late Arriving"),
        ]
        audit_df = pd.DataFrame(
            [{"Metric": label, "Count": batch.get(key, 0)} for key, label in audit_fields]
        )

        # Colour non-zero anomaly rows
        def _highlight(row):
            anomaly_keys = {"Rejected", "Flagged", "Late Arriving"}
            if row["Metric"] in anomaly_keys and row["Count"] > 0:
                return ["background-color: #FEF2F2; color: #991B1B"] * 2
            if row["Metric"] == "Inserted" and row["Count"] > 0:
                return ["background-color: #F0FDF4; color: #166534"] * 2
            return [""] * 2

        st.dataframe(
            audit_df.style.apply(_highlight, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        if latest_mode == "incremental" and batch.get("deduped", 0) > 0:
            st.info(
                f"ℹ️  **{batch['deduped']} rows deduplicated** — this is expected when "
                "re-running incremental over the same files.  The pipeline converges to a "
                "stable state without double-counting."
            )

st.divider()

# ── Weekly Batch Generator ────────────────────────────────────────────────────

st.subheader("🗓 Weekly Batch Generator")
st.caption(
    "Generate a new incremental `.xlsx` file for the **current ISO week** (Monday start). "
    "If a file for this week already exists in `data/input/incremental/`, generation is "
    "skipped — no data is overwritten.  After generating, run **Incremental Load** above to "
    "ingest the new file."
)

from datetime import date
from datetime import timedelta as _td

_today = date.today()
_week_mon = _today - _td(days=_today.weekday())
st.info(
    f"Current week starts **{_week_mon}** — files would be named `{_week_mon}_pos.xlsx` and `{_week_mon}_online.xlsx`."
)

bg1, bg2 = st.columns(2)

with bg1:
    st.markdown("#### 🛒 POS Batch")
    st.markdown(
        "Generates ~700 in-store transactions for the week with a `Sales` sheet "
        "(Schema A: `transaction_id / ts / store_id / sku / qty / unit_price / amount / currency`).  "
        "Includes ~8% injected DQ faults (zero-qty, duplicate rows) caught by the pre-ingestion DQ gate."
    )
    pos_btn = st.button("Generate POS Batch", key="gen_pos", use_container_width=True)

with bg2:
    st.markdown("#### 🌐 Online Batch")
    st.markdown(
        "Generates ~500 e-commerce orders for the week with an `Orders` sheet "
        "(Schema B: `order_id / order_datetime / location_id / product_sku / units / price_per_unit / currency`).  "
        "Includes ~8% injected DQ faults caught by the pre-ingestion DQ gate."
    )
    online_btn = st.button("Generate Online Batch", key="gen_online", use_container_width=True)

both_btn = st.button(
    "⚡ Generate Both (POS + Online) for This Week",
    key="gen_both",
    type="primary",
    use_container_width=True,
)


def _run_generate(batch_type: str):
    try:
        with st.spinner(f"Generating {batch_type.upper()} batch for week of {_week_mon}…"):
            result = api_client.post_generate_batch(batch_type)
        st.session_state[f"gen_{batch_type}_result"] = result
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot reach the API. Is the server running?")
    except requests.exceptions.Timeout:
        st.error("❌ Request timed out while generating batch.")
    except requests.exceptions.HTTPError as e:
        st.error(f"❌ API error {e.response.status_code}: {e.response.text[:400]}")
    except Exception as e:
        st.error(f"❌ Unexpected error: {type(e).__name__}: {e}")


if pos_btn:
    _run_generate("pos")

if online_btn:
    _run_generate("online")

if both_btn:
    _run_generate("pos")
    _run_generate("online")

for _bt in ("pos", "online"):
    _res = st.session_state.get(f"gen_{_bt}_result")
    if _res:
        if _res["status"] == "exists":
            st.warning(
                f"**{_bt.upper()} batch already exists** for week `{_res['week_start']}` → "
                f"`{_res['file']}`.  No new file was written.  "
                "Run Incremental Load above if you haven't already."
            )
        else:
            st.success(
                f"**{_bt.upper()} batch created!** → `{_res['file']}` ({_res['rows']:,} rows)"
            )
            dq = _res.get("dq", {})
            if dq:
                dq_cols = st.columns(len(dq))
                for col, (issue, cnt) in zip(dq_cols, dq.items(), strict=False):
                    col.metric(issue.replace("_", " ").title(), cnt)
                st.caption(
                    "These faults are **intentionally injected** for testing — they will be "
                    "caught and quarantined by the DQ gate during Incremental Load."
                )
            st.markdown(f"File saved to: `{_res['path']}`")

st.divider()

# ── Historical load log (from /quality) ──────────────────────────────────────

st.subheader("📋 Pipeline History")

try:
    quality = api_client.get_quality()
    total = quality["total_batches"]
    lb = quality.get("latest_batch")

    h1, h2 = st.columns(2)
    h1.metric("Total Batches Run", total)
    h2.metric("Latest Batch Inserted", lb["inserted"] if lb else "—")

    if lb:
        st.markdown("**Latest batch details:**")
        detail_df = pd.DataFrame(
            [
                {
                    "Field": k.replace("_", " ").title(),
                    "Value": v,
                }
                for k, v in lb.items()
            ]
        )
        st.dataframe(detail_df, use_container_width=True, hide_index=True)
    elif total == 0:
        st.info("No load batches recorded yet. Click a load button above to get started.")
except requests.exceptions.ConnectionError:
    st.warning("Could not load pipeline history — API unreachable.")
except Exception as exc:
    st.warning(f"Could not load pipeline history: {exc}")
