"""
ui/app.py

Streamlit entry-point.  Uses st.navigation so all pages share one sidebar.
Run locally:
    streamlit run ui/app.py
In Docker:
    Set API_BASE_URL=http://api:8000 via env or .env
"""
import sys
from pathlib import Path

# Make ui/ importable from every page (pages run as scripts, not modules)
_UI_DIR = Path(__file__).resolve().parent
if str(_UI_DIR) not in sys.path:
    sys.path.insert(0, str(_UI_DIR))

import streamlit as st

st.set_page_config(
    page_title="CPG Analytics",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded",
)

pg = st.navigation(
    {
        "Analytics": [
            st.Page("pages/dashboard.py",  title="Dashboard",      icon="📊"),
            st.Page("pages/forecast.py",   title="Forecast",       icon="📈"),
        ],
        "AI": [
            st.Page("pages/insights.py",   title="AI Insights & Q&A", icon="🤖"),
        ],
        "Operations": [
            st.Page("pages/data_loads.py", title="Data Loads",     icon="🔄"),
        ],
    }
)

pg.run()
