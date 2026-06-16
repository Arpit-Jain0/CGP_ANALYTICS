"""
ui/api_client.py

Thin HTTP client that wraps every FastAPI endpoint.
Read API_BASE_URL from the environment (default: http://localhost:8000).
All errors are surfaced as exceptions so callers can wrap in st.error().
"""
from __future__ import annotations

import os
import requests

_BASE    = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
_TIMEOUT = 120   # seconds — historical ingest can be slow


def _get(path: str, **params) -> dict:
    filtered = {k: v for k, v in params.items() if v is not None}
    r = requests.get(f"{_BASE}{path}", params=filtered, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(path: str, json: dict | None = None, **params) -> dict:
    filtered = {k: v for k, v in params.items() if v is not None}
    r = requests.post(f"{_BASE}{path}", json=json, params=filtered, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def health() -> dict:
    return _get("/health")


def get_summary(start_date=None, end_date=None) -> dict:
    return _get(
        "/summary",
        start_date=str(start_date) if start_date else None,
        end_date=str(end_date) if end_date else None,
    )


def get_quality() -> dict:
    return _get("/quality")


def get_forecast(
    category: str | None = None,
    region: str | None = None,
    horizon: int = 90,
) -> dict:
    return _get("/forecast", category=category, region=region, horizon=horizon)


def post_insights() -> dict:
    return _post("/insights")


def post_ask(question: str) -> dict:
    return _post("/ask", json={"question": question})


def post_ingest(mode: str) -> dict:
    return _post("/ingest", mode=mode)
