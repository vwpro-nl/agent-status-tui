"""Codex adapter.

Capacity: the proven acquisition path from ``codex-status-tui`` -- a bounded,
short-lived ``codex app-server --stdio`` JSON-RPC ``account/rateLimits/read``
(genuinely LIVE), falling back to the last ``token_count`` event's embedded
``rate_limits`` block in the newest local session rollout files.

Activity: session rollout file mtimes + the per-prompt ``history.jsonl`` + the
newest ``threads.updated_at`` in ``state_5.sqlite`` -- combined with ``max()``.
The standalone app-server rate-limit read writes no rollout and appends no
history entry, so polling never inflates the activity signal.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from ..model import FIVE_HOUR_MINUTES, WEEK_MINUTES, AgentStatus, Detection, Window
from ..env import Env
from ._common import combine, last_json_value, newest_mtime, parse_iso

KEY = "codex"
DISPLAY_NAME = "CODEX"

_APP_SERVER_TIMEOUT = 5.0
_ROLLOUT_SCAN = 30


# --- detection / activity --------------------------------------------------


def _rollout_files(env: Env):
    root = env.codex_home / "sessions"
    if not root.is_dir():
        return []
    try:
        return list(root.rglob("rollout-*.jsonl"))
    except OSError:
        return []


def _sqlite_thread_activity(env: Env) -> float | None:
    db = env.codex_home / "state_5.sqlite"
    if not db.is_file():
        return None
    try:
        uri = f"file:{db}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as conn:
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                "SELECT MAX(COALESCE(updated_at_ms / 1000.0, updated_at)) FROM threads"
            ).fetchone()
    except (sqlite3.Error, OSError):
        return None
    if not row or row[0] is None:
        return None
    try:
        value = float(row[0])
    except (TypeError, ValueError):
        return None
    return value / 1000.0 if value > 10_000_000_000 else value


def detect(env: Env) -> Detection:
    installed = bool(shutil.which("codex")) or env.codex_home.is_dir()
    rollouts = _rollout_files(env)
    history = env.codex_home / "history.jsonl"
    ever_used = bool(rollouts) or (history.is_file() and history.stat().st_size > 0)

    last_activity = combine(
        newest_mtime(iter(rollouts)),
        last_json_value(history, "ts") if history.is_file() else None,
        _sqlite_thread_activity(env),
    )
    return Detection(installed=installed, ever_used=ever_used, last_activity=last_activity)


# --- live app-server acquisition (ported from codex-status-tui) ---------------


def _normalize_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    if not isinstance(used, (int, float)):
        return None
    normalized: dict[str, Any] = {"used_percent": float(used)}
    resets_at = window.get("resetsAt")
    if isinstance(resets_at, (int, float)):
        normalized["resets_at"] = float(resets_at)
    duration = window.get("windowDurationMins")
    if isinstance(duration, (int, float)):
        normalized["window_minutes"] = float(duration)
    return normalized


def _app_server_snapshot(env: Env) -> dict[str, dict[str, Any]] | None:
    codex = shutil.which("codex")
    if codex is None:
        return None
    try:
        process = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None
    if process.stdin is None or process.stdout is None:
        process.kill()
        return None

    def send(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-status-tui", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        deadline = time.monotonic() + _APP_SERVER_TIMEOUT
        initialized = False
        while time.monotonic() < deadline:
            raw = process.stdout.readline()
            if not raw:
                return None
            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(message, dict) and message.get("id") == 1:
                initialized = True
                break
        if not initialized:
            return None

        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})

        response: dict[str, Any] | None = None
        deadline = time.monotonic() + _APP_SERVER_TIMEOUT
        while time.monotonic() < deadline:
            raw = process.stdout.readline()
            if not raw:
                break
            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(message, dict) and message.get("id") == 2:
                response = message
                break
        if response is None:
            return None

        result = response.get("result")
        if not isinstance(result, dict):
            return None

        snapshots = result.get("rateLimitsByLimitId")
        if not isinstance(snapshots, dict):
            snapshot = result.get("rateLimits")
            if not isinstance(snapshot, dict):
                return None
            snapshots = {"codex": snapshot}

        limits: dict[str, dict[str, Any]] = {}
        for fallback_id, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue
            limit_id = snapshot.get("limitId")
            if not isinstance(limit_id, str) or not limit_id:
                limit_id = fallback_id if isinstance(fallback_id, str) else "codex"
            normalized: dict[str, Any] = {}
            primary = _normalize_window(snapshot.get("primary"))
            secondary = _normalize_window(snapshot.get("secondary"))
            if primary is not None:
                normalized["primary"] = primary
            if secondary is not None:
                normalized["secondary"] = secondary
            if normalized:
                limits[limit_id] = normalized
        return limits or None
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# --- local rollout fallback (ported from codex-status-tui) -----------------


def _inspect_rollout(path: Path) -> tuple[dict[str, Any] | None, float | None]:
    from ._common import reverse_lines

    for raw in reverse_lines(path):
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        rate = payload.get("rate_limits")
        if not isinstance(rate, dict):
            continue
        return rate, parse_iso(record.get("timestamp"))
    return None, None


def _local_snapshot(env: Env) -> tuple[dict[str, Any] | None, float | None]:
    files = _rollout_files(env)
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    for path in files[:_ROLLOUT_SCAN]:
        rate, timestamp = _inspect_rollout(path)
        if rate:
            return rate, timestamp
    return None, None


# --- normalization --------------------------------------------------------


def _window(raw: Any, default_minutes: int) -> Window | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    if not isinstance(used, (int, float)):
        return None
    minutes = raw.get("window_minutes")
    nominal = int(minutes) if isinstance(minutes, (int, float)) and minutes > 0 else default_minutes
    resets_at = raw.get("resets_at")
    return Window(
        used_percent=max(0.0, min(100.0, float(used))),
        resets_at=float(resets_at) if isinstance(resets_at, (int, float)) else None,
        nominal_minutes=nominal,
    )


def _pick_bucket(limits: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if "codex" in limits:
        return limits["codex"]
    return next(iter(limits.values()), {})


def poll(env: Env, now: float) -> AgentStatus:
    detection = detect(env)

    live = _app_server_snapshot(env)
    if live:
        bucket = _pick_bucket(live)
        return AgentStatus(
            key=KEY,
            display_name=DISPLAY_NAME,
            five_hour=_window(bucket.get("primary"), FIVE_HOUR_MINUTES),
            weekly=_window(bucket.get("secondary"), WEEK_MINUTES),
            freshness_kind="live",
            source_age=None,
            last_activity=detection.last_activity,
            availability="ok",
        )

    rate, event_ts = _local_snapshot(env)
    if rate:
        return AgentStatus(
            key=KEY,
            display_name=DISPLAY_NAME,
            five_hour=_window(rate.get("primary"), FIVE_HOUR_MINUTES),
            weekly=_window(rate.get("secondary"), WEEK_MINUTES),
            freshness_kind="cache",
            source_age=(now - event_ts) if event_ts is not None else None,
            last_activity=detection.last_activity,
            availability="ok",
        )

    return AgentStatus(
        key=KEY,
        display_name=DISPLAY_NAME,
        five_hour=None,
        weekly=None,
        freshness_kind="none",
        source_age=None,
        last_activity=detection.last_activity,
        availability="no-capacity-data",
        detail="no app-server or local rate-limit data",
    )
