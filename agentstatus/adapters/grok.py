"""Grok adapter.

Activity, in preference order:
  1. ``~/.grok/sessions/**/summary.json`` -> ``last_active_at`` / ``updated_at``
  2. newest session-file mtime
  3. ``~/.grok/active_sessions.json`` -> ``opened_at``

Weekly quota is fetched live: a single bounded, read-only
``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`` authorised
with the current short-lived bearer token from ``~/.grok/auth.json``.

Credential handling is strict:
  * ``~/.grok/auth.json`` is read, never written;
  * only a non-empty string ``key`` is used, and only if its ``expires_at``
    has not passed -- an expired token is never sent;
  * ``refresh_token`` is never read or used; no OAuth refresh is attempted;
  * the token is never logged, persisted, or placed in any error/detail text.
A failure at any step (no credential, expired, HTTP, network, JSON, schema)
leaves activity detection untouched and simply yields no weekly window.
"""

from __future__ import annotations

import json
import shutil

from ..grok_billing import (
    BILLING_MAX_BYTES as _BILLING_MAX_BYTES,
    BILLING_TIMEOUT as _BILLING_TIMEOUT,
    BILLING_URL as _BILLING_URL,
    WEEKLY_PERIOD as _WEEKLY_PERIOD,
    billing_request as _billing_request,
    safe_close as _safe_close,
    select_bearer as _select_bearer,
)
from ..model import WEEK_MINUTES, AgentStatus, Detection, Window
from ..env import Env
from ._common import combine, load_json, newest_mtime, parse_iso

KEY = "grok"
DISPLAY_NAME = "GROK"

_SESSION_GLOBS = ("*.jsonl", "*.json")


# --- activity detection (unchanged) ---------------------------------------


def _sessions_root(env: Env):
    root = env.grok_home / "sessions"
    return root if root.is_dir() else None


def _summary_activity(root) -> float | None:
    best: float | None = None
    try:
        summaries = list(root.rglob("summary.json"))
    except OSError:
        return None
    for path in summaries[:2000]:
        data = load_json(path)
        if not isinstance(data, dict):
            continue
        candidate = combine(
            parse_iso(data.get("last_active_at")),
            parse_iso(data.get("updated_at")),
        )
        if candidate is not None and (best is None or candidate > best):
            best = candidate
    return best


def _session_file_activity(root) -> float | None:
    files = []
    for pattern in _SESSION_GLOBS:
        try:
            files.extend(root.rglob(pattern))
        except OSError:
            pass
    return newest_mtime(iter(files))


def _active_sessions_activity(env: Env) -> float | None:
    data = load_json(env.grok_home / "active_sessions.json")
    if not isinstance(data, list):
        return None
    best: float | None = None
    for entry in data:
        if not isinstance(entry, dict):
            continue
        candidate = parse_iso(entry.get("opened_at"))
        if candidate is not None and (best is None or candidate > best):
            best = candidate
    return best


def detect(env: Env) -> Detection:
    installed = env.grok_home.is_dir() or bool(shutil.which("grok"))
    root = _sessions_root(env)
    ever_used = False
    if root is not None:
        try:
            ever_used = any(root.iterdir())
        except OSError:
            ever_used = False

    # Preference order: summary.json's own field is authoritative; only fall
    # back to file mtimes / active_sessions when no summary is present.
    last_activity = _summary_activity(root) if root is not None else None
    if last_activity is None:
        fallbacks = [_active_sessions_activity(env)]
        if root is not None:
            fallbacks.append(_session_file_activity(root))
        last_activity = combine(*fallbacks)

    return Detection(installed=installed, ever_used=ever_used, last_activity=last_activity)


# --- live weekly quota ---------------------------------------------------


def _map_weekly(payload) -> tuple[Window | None, str | None]:
    """Map a billing payload to a weekly :class:`Window`, or explain why not.

    A window is produced only when the period is explicitly weekly and the
    percentage is a real number.  A missing/null/malformed percentage yields
    no window (never a fabricated 0%); a missing/invalid period end yields a
    window with ``resets_at=None`` (never a fabricated reset).
    """
    if not isinstance(payload, dict):
        return None, "billing response malformed"
    config = payload.get("config")
    if not isinstance(config, dict):
        return None, "billing response malformed"

    period = config.get("currentPeriod")
    if not isinstance(period, dict) or period.get("type") != _WEEKLY_PERIOD:
        return None, "billing period not weekly"

    percent = config.get("creditUsagePercent")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None, "billing percent unusable"

    used_percent = max(0.0, min(100.0, float(percent)))
    resets_at = parse_iso(period.get("end"))
    starts_at = parse_iso(period.get("start"))
    if resets_at is not None and starts_at is not None and resets_at > starts_at:
        nominal_minutes = int(round((resets_at - starts_at) / 60))
    else:
        nominal_minutes = WEEK_MINUTES

    return Window(used_percent=used_percent, resets_at=resets_at,
                  nominal_minutes=nominal_minutes), None


def _weekly_quota(env: Env, now: float) -> tuple[Window | None, str | None]:
    token = _select_bearer(env, now)
    if token is None:
        return None, "grok credential unavailable or expired"
    try:
        payload = _billing_request(token)
    except Exception as exc:
        # Deliberately swallow everything: the exception could carry response
        # text and must never reach a log or the row's detail.
        _safe_close(exc)
        return None, "grok billing request failed"
    finally:
        token = None  # drop the reference promptly

    window, reason = _map_weekly(payload)
    return window, (None if window is not None else f"grok {reason}")


# --- poll --------------------------------------------------------------


def poll(env: Env, now: float) -> AgentStatus:
    detection = detect(env)
    weekly, note = _weekly_quota(env, now)

    if weekly is not None:
        return AgentStatus(
            key=KEY,
            display_name=DISPLAY_NAME,
            five_hour=None,          # still no 5h source for Grok
            weekly=weekly,
            freshness_kind="live",   # a genuine live billing read
            source_age=None,
            last_activity=detection.last_activity,
            availability="ok",
        )

    age = (now - detection.last_activity) if detection.last_activity is not None else None
    return AgentStatus(
        key=KEY,
        display_name=DISPLAY_NAME,
        five_hour=None,
        weekly=None,
        freshness_kind="activity" if age is not None else "none",
        source_age=age,
        last_activity=detection.last_activity,
        availability="no-capacity-data",
        detail=note or "no local 5h/week quota source",
    )
