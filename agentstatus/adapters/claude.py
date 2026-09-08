"""Claude adapter.

Capacity: the proven zero-network, zero-credential logic from
``claude-status-tui`` -- the Omarchy agent-usage cache first, then
``~/.claude.json``'s ``cachedUsageUtilization``.  Claude is *never* LIVE; the
data column always shows the source snapshot's own age.

Activity: newest transcript mtime under ``~/.claude/projects`` plus the
per-prompt ``~/.claude/history.jsonl``.  The Omarchy ``fetchedAtMs`` and the
``~/.claude.json`` rewrite time are deliberately NOT used as activity -- they
move for reasons unrelated to the user.
"""

from __future__ import annotations

from typing import Any

from ..model import FIVE_HOUR_MINUTES, WEEK_MINUTES, AgentStatus, Detection, Window
from ..env import Env
from ._common import combine, last_json_value, load_json, newest_mtime, parse_iso

KEY = "claude"
DISPLAY_NAME = "CLAUDE"

# Two independently-observed staleness ceilings, carried over verbatim: the
# Omarchy collector refreshes on a 900s schedule; cachedUsageUtilization has no
# discovered on-demand refresh.
_OMARCHY_STALE_AFTER = 1800
_CLAUDE_JSON_STALE_AFTER = 9000


# --- detection / activity --------------------------------------------------


def _transcript_files(env: Env):
    if not env.claude_projects.is_dir():
        return []
    try:
        return list(env.claude_projects.rglob("*.jsonl"))
    except OSError:
        return []


def detect(env: Env) -> Detection:
    installed = env.claude_json.is_file() or env.claude_home.is_dir()
    transcripts = _transcript_files(env)
    history = env.claude_home / "history.jsonl"
    ever_used = bool(transcripts) or (history.is_file() and history.stat().st_size > 0)

    last_activity = combine(
        newest_mtime(iter(transcripts)),
        last_json_value(history, "timestamp") if history.is_file() else None,
    )
    return Detection(installed=installed, ever_used=ever_used, last_activity=last_activity)


# --- capacity acquisition (ported from claude-status-tui) ------------------


def _read_omarchy(env: Env) -> dict[str, Any] | None:
    data = load_json(env.omarchy_claude_cache)
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("fetchedAtMs"), (int, float)):
        return None
    if not isinstance(data.get("limits"), list):
        return None
    return data


def _read_claude_json(env: Env) -> dict[str, Any] | None:
    data = load_json(env.claude_json)
    if not isinstance(data, dict):
        return None
    cached = data.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None
    if not isinstance(cached.get("fetchedAtMs"), (int, float)):
        return None
    if not isinstance(cached.get("utilization"), dict):
        return None
    return cached


def _normalize_omarchy(data: dict[str, Any], now: float) -> dict[str, Window]:
    windows: dict[str, Window] = {}
    for entry in data.get("limits", []):
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        if not isinstance(label, str):
            continue
        if "5-hour" in label:
            kind, minutes = "five_hour", FIVE_HOUR_MINUTES
        elif "7-day" in label:
            kind, minutes = "weekly", WEEK_MINUTES
        else:
            continue
        percent = entry.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        resets_at = parse_iso(entry.get("resetsAt"))
        if resets_at is not None and resets_at <= now:  # expired -> drop
            continue
        windows[kind] = Window(
            used_percent=max(0.0, min(100.0, float(percent) * 100)),
            resets_at=resets_at,
            nominal_minutes=minutes,
        )
    return windows


def _normalize_claude_json(cached: dict[str, Any], now: float) -> dict[str, Window]:
    utilization = cached.get("utilization")
    windows: dict[str, Window] = {}
    if not isinstance(utilization, dict):
        return windows
    for src_key, kind, minutes in (
        ("five_hour", "five_hour", FIVE_HOUR_MINUTES),
        ("seven_day", "weekly", WEEK_MINUTES),
    ):
        entry = utilization.get(src_key)
        if not isinstance(entry, dict):
            continue
        used = entry.get("utilization")
        if not isinstance(used, (int, float)):
            continue
        resets_at = parse_iso(entry.get("resets_at"))
        if resets_at is not None and resets_at <= now:  # expired -> drop
            continue
        windows[kind] = Window(
            used_percent=max(0.0, min(100.0, float(used))),
            resets_at=resets_at,
            nominal_minutes=minutes,
        )
    return windows


def _load_snapshot(env: Env, now: float) -> tuple[dict[str, Window], str | None, float | None]:
    omarchy_raw = _read_omarchy(env)
    if omarchy_raw is not None:
        windows = _normalize_omarchy(omarchy_raw, now)
        if windows:
            return windows, "omarchy", float(omarchy_raw["fetchedAtMs"]) / 1000

    claude_raw = _read_claude_json(env)
    if claude_raw is not None:
        windows = _normalize_claude_json(claude_raw, now)
        if windows:
            return windows, "claude_json", float(claude_raw["fetchedAtMs"]) / 1000

    if omarchy_raw is not None:
        return {}, "omarchy", float(omarchy_raw["fetchedAtMs"]) / 1000
    if claude_raw is not None:
        return {}, "claude_json", float(claude_raw["fetchedAtMs"]) / 1000
    return {}, None, None


def poll(env: Env, now: float) -> AgentStatus:
    detection = detect(env)
    windows, source, fetched_at = _load_snapshot(env, now)

    if source is None:
        return AgentStatus(
            key=KEY,
            display_name=DISPLAY_NAME,
            five_hour=None,
            weekly=None,
            freshness_kind="none",
            source_age=None,
            last_activity=detection.last_activity,
            availability="no-capacity-data",
            detail="no omarchy cache or cachedUsageUtilization",
        )

    return AgentStatus(
        key=KEY,
        display_name=DISPLAY_NAME,
        five_hour=windows.get("five_hour"),
        weekly=windows.get("weekly"),
        freshness_kind="cache",  # Claude is never live
        source_age=(now - fetched_at) if fetched_at is not None else None,
        last_activity=detection.last_activity,
        availability="ok" if windows else "no-capacity-data",
    )
