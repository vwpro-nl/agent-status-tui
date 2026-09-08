"""Builders for throwaway fake home directories.  No real files, no credentials."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from agentstatus.env import Env

STRIP_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return STRIP_ANSI.sub("", text)


def _write(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def make_env(root: Path) -> Env:
    return Env(
        codex_home=root / "codex",
        claude_home=root / "claude",
        claude_json=root / "claude.json",
        claude_projects=root / "claude" / "projects",
        omarchy_claude_cache=root / "omarchy" / "claude-limits.json",
        grok_home=root / "grok",
    )


# --- codex ---------------------------------------------------------------


def codex_rollout(env: Env, *, primary_pct, secondary_pct, now, event_age=120,
                  five_hour_left=29 * 60, week_left=34 * 3600):
    rate = {
        "primary": {
            "used_percent": primary_pct,
            "resets_at": now + five_hour_left,
            "window_minutes": 300,
        },
        "secondary": {
            "used_percent": secondary_pct,
            "resets_at": now + week_left,
            "window_minutes": 10080,
        },
    }
    record = {
        "type": "event_msg",
        "timestamp": iso(now - event_age),
        "payload": {"type": "token_count", "rate_limits": rate},
    }
    _write(
        env.codex_home / "sessions" / "2026" / "09" / "05" / "rollout-abc.jsonl",
        json.dumps(record) + "\n",
        mtime=now - event_age,
    )


def codex_history(env: Env, *, ts):
    _write(
        env.codex_home / "history.jsonl",
        json.dumps({"session_id": "s", "ts": int(ts), "text": "do a thing"}) + "\n",
        mtime=ts,
    )


# --- claude -------------------------------------------------------------


def claude_omarchy(env: Env, *, fetched_at, five_pct=0.63, week_pct=0.62,
                   now=None, five_left=18 * 60, week_left=52 * 3600, five_reset=None):
    now = fetched_at if now is None else now
    if five_reset is None:
        five_reset = iso(now + five_left)
    payload = {
        "fetchedAtMs": int(fetched_at * 1000),
        "limits": [
            {"label": "Session (5-hour)", "percent": five_pct, "resetsAt": five_reset},
            {"label": "Weekly (7-day)", "percent": week_pct, "resetsAt": iso(now + week_left)},
        ],
    }
    _write(env.omarchy_claude_cache, json.dumps(payload))


def claude_json_cache(env: Env, *, fetched_at, five_pct=27, week_pct=49, now=None,
                      five_left=18 * 60, week_left=52 * 3600):
    now = fetched_at if now is None else now
    payload = {
        "cachedUsageUtilization": {
            "fetchedAtMs": int(fetched_at * 1000),
            "utilization": {
                "five_hour": {"utilization": five_pct, "resets_at": iso(now + five_left)},
                "seven_day": {"utilization": week_pct, "resets_at": iso(now + week_left)},
            },
        }
    }
    _write(env.claude_json, json.dumps(payload))


def claude_activity(env: Env, *, transcript_ts=None, history_ts=None):
    if transcript_ts is not None:
        p = _write(
            env.claude_projects / "-home-x" / "session.jsonl",
            json.dumps({"type": "assistant", "timestamp": iso(transcript_ts)}) + "\n",
        )
        import os

        os.utime(p, (transcript_ts, transcript_ts))
    if history_ts is not None:
        _write(
            env.claude_home / "history.jsonl",
            json.dumps({"display": "hi", "timestamp": int(history_ts * 1000),
                        "project": "/x"}) + "\n",
        )


# --- grok --------------------------------------------------------------


def grok_session(env: Env, *, last_active_at=None, updated_at=None, opened_at=None,
                 file_mtime=None):
    """Build a Grok session dir.  ``file_mtime`` controls the on-disk mtimes
    independently of ``last_active_at`` so tests can prove that summary.json's
    field -- not the file mtime -- is the preferred activity signal."""
    sdir = env.grok_home / "sessions" / "%2Fhome%2Fx" / "01a0-session"
    stamp = file_mtime if file_mtime is not None else (last_active_at or updated_at)
    if last_active_at is not None or updated_at is not None:
        data = {}
        if last_active_at is not None:
            data["last_active_at"] = iso(last_active_at)
        if updated_at is not None:
            data["updated_at"] = iso(updated_at)
        _write(sdir / "summary.json", json.dumps(data), mtime=stamp)
    else:
        _write(sdir / "events.jsonl", json.dumps({"ts": iso(0)}) + "\n", mtime=stamp)
    if opened_at is not None:
        _write(
            env.grok_home / "active_sessions.json",
            json.dumps([{"session_id": "x", "opened_at": iso(opened_at)}]),
            mtime=stamp,
        )


def grok_auth(env: Env, *, entries=None, key="grok-bearer-token-value",
              expires_at=None, raw=None):
    """Write ~/.grok/auth.json.  ``raw`` writes verbatim text (for malformed
    JSON).  Otherwise ``entries`` (list of dicts) or a single entry built from
    ``key`` / ``expires_at`` is written under an auth.x.ai-style top key."""
    path = env.grok_home / "auth.json"
    if raw is not None:
        return _write(path, raw)
    if entries is None:
        entry = {"key": key, "refresh_token": "REFRESH-TOKEN-MUST-NOT-BE-USED",
                 "auth_mode": "oidc"}
        if expires_at is not None:
            entry["expires_at"] = iso(expires_at)
        entries = [entry]
    doc = {
        f"https://auth.x.ai::{i}": entry for i, entry in enumerate(entries)
    }
    return _write(path, json.dumps(doc))


WEEKLY_BILLING = {
    "config": {
        "creditUsagePercent": 3.0,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-09-05T16:26:02.206145+00:00",
            "end": "2026-09-12T16:26:02.206145+00:00",
        },
    }
}
