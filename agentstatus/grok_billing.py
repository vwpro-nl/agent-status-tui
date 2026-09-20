"""Shared Grok billing/auth helpers for the dashboard's Grok adapter.

This is not a public API. Keep the same credential rules as the rest of the
project: read-only auth.json, only a non-empty ``key``, never
``refresh_token``, never log or surface the bearer. (The self-contained
``agentstatus.keepalive`` package intentionally does not import this module
-- see its own package docstring -- and reimplements the same minimal
subset directly in ``agentstatus/keepalive/observe.py``.)
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.request
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any | None:
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
BILLING_TIMEOUT = 4.0
BILLING_MAX_BYTES = 1_000_000
WEEKLY_PERIOD = "USAGE_PERIOD_TYPE_WEEKLY"


def select_bearer(env: Any, now: float) -> str | None:
    """A currently-valid bearer token from ``auth.json``, or ``None``.

    Reads the file read-only. Considers only entries with a non-empty string
    ``key``; skips any whose parseable ``expires_at`` is in the past; prefers
    the entry with the latest ``expires_at``. Never inspects ``refresh_token``.
    The returned value is handed straight to the request builder and to
    nothing else.
    """
    data = _load_json(env.grok_home / "auth.json")
    if not isinstance(data, dict):
        return None

    best_rank = None
    best_key: str | None = None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        expires_at = _parse_iso(entry.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        rank = expires_at if expires_at is not None else float("-inf")
        if best_rank is None or rank > best_rank:
            best_rank, best_key = rank, key
    return best_key


def billing_request(token: str):
    """Bounded billing GET. Raises on HTTP/network/decode errors."""
    request = urllib.request.Request(
        BILLING_URL,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=BILLING_TIMEOUT) as response:
        raw = response.read(BILLING_MAX_BYTES)
    return json.loads(raw.decode("utf-8", "replace"))


def safe_close(obj) -> None:
    """Close a file-like exception payload (e.g. urllib's HTTPError)."""
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass
