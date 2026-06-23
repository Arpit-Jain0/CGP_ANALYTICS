"""Page — API Documentation: embedded Swagger UI with endpoint reference."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import streamlit.components.v1 as components

import api_client

# ── Header ────────────────────────────────────────────────────────────────────

st.title("🔌 API Documentation")
st.caption(
    "FastAPI auto-generates this documentation from the code — "
    "it is always in sync with what the API actually does."
)

# ── Status check ──────────────────────────────────────────────────────────────

try:
    api_client.health()
    st.success("API is online", icon="✅")
except Exception:
    st.error("❌ Cannot reach the API — start it with: `docker compose up api`")
    st.stop()

st.link_button("Open in Full Tab →", url="http://localhost:8000/docs", use_container_width=False)
st.divider()

# ── Endpoint reference table ──────────────────────────────────────────────────

st.markdown("#### Endpoint Reference")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Analytics**")
    st.markdown("""
| Method | Endpoint | What it returns |
|---|---|---|
| `GET` | `/health` | API status |
| `GET` | `/summary` | Revenue KPIs, top category, top region |
| `GET` | `/quality` | DQ issue counts, batch history |
| `GET` | `/products` | Top products by revenue |
| `GET` | `/forecast` | Prophet predictions for a category/region |
""")

    st.markdown("**Operations**")
    st.markdown("""
| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/ingest` | Runs the ingestion pipeline (historical or incremental) |
| `POST` | `/generate-batch` | Generates a new incremental Excel batch file |
""")

with col2:
    st.markdown("**AI**")
    st.markdown("""
| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/insights` | Generates a revenue narrative via Ollama |
| `POST` | `/ask` | Answers a plain-English question about the data |
""")

    st.markdown("**Database Explorer**")
    st.markdown("""
| Method | Endpoint | What it returns |
|---|---|---|
| `GET` | `/db/overview` | All schemas, tables, and row counts |
| `GET` | `/db/table` | Paginated rows from any table |
""")

    st.markdown("**DQ Reports**")
    st.markdown("""
| Method | Endpoint | What it returns |
|---|---|---|
| `GET` | `/dq-reports` | List of all DQ report files |
| `GET` | `/dq-reports/{filename}` | Rows from one DQ report |
""")

st.divider()

# ── Embedded Swagger UI ────────────────────────────────────────────────────────

st.markdown("#### Interactive Docs (Swagger UI)")
st.caption("Try any endpoint directly — click an endpoint, hit 'Try it out', then 'Execute'.")

components.iframe("http://localhost:8000/docs", height=820, scrolling=True)
