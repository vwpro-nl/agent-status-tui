"""Chronological, per-agent keepalive event history with bounded retention.

One append-only JSONL file per agent (mirrors the per-agent state file
convention already used elsewhere in this package): a single writer per
file -- each agent's own scheduler thread -- so there is never a
cross-agent write race and never a lock to manage. A chronological,
all-agents view is produced by merging and sorting the per-agent files at
read time (:func:`merge_chronological`), not by sharing one file.

Every write (:func:`record_event`) is a single atomic temp-file-plus-
``os.replace`` rewrite of that agent's own file: crash-safe, and it also
enforces the 16-day retention window in the same atomic step, so no
separate cleanup pass or cron job is needed -- retention simply happens as
a side effect of normal keepalive operation recording its own events.

Reading (:func:`load_events`) is deliberately tolerant: a line that is not
valid JSON, not an object, or carries the wrong schema is skipped rather
than raised. Corrupt history must never disable keepalive -- neither by
blocking a new event from being recorded (the next successful
``record_event`` rewrites the file with only the valid events it could
recover, self-healing it) nor by crashing ``history``/``monitor`` display.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

HISTORY_SCHEMA = "agent-status-keepalive-history/v1"
RETENTION_DAYS = 16


class HistoryError(Exception):
    pass


def history_dir(state_dir: Path) -> Path:
    return state_dir / "history"


def history_path(state_dir: Path, agent_key: str) -> Path:
    return history_dir(state_dir) / f"{agent_key}.jsonl"


def _event_time(event: dict[str, Any]) -> dt.datetime | None:
    value = event.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def load_events(path: Path) -> list[dict[str, Any]]:
    """Every structurally valid event in ``path``, oldest-first as stored.

    Never raises: a missing file is an empty history, and any unreadable or
    malformed line is silently skipped rather than failing the whole read.
    Use :func:`load_events_with_stats` when the count of skipped/invalid
    lines itself is needed (e.g. Doctor).
    """
    events, _skipped = load_events_with_stats(path)
    return events


def load_events_with_stats(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Like :func:`load_events`, plus how many lines were skipped as invalid."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], 0
    except OSError:
        return [], 0
    events: list[dict[str, Any]] = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        if not isinstance(value, dict) or value.get("schema") != HISTORY_SCHEMA:
            skipped += 1
            continue
        events.append(value)
    return events, skipped


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events
    ).encode()
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        if os.write(fd, payload) != len(payload):
            raise OSError("short history write")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def prune(events: Iterable[dict[str, Any]], now: dt.datetime,
         retention_days: int = RETENTION_DAYS) -> list[dict[str, Any]]:
    """Keep only events at or after ``now - retention_days``.

    An event with no parseable timestamp is kept rather than silently
    dropped -- retention only ever removes what it can positively identify
    as old.
    """
    cutoff = now - dt.timedelta(days=retention_days)
    kept = []
    for event in events:
        when = _event_time(event)
        if when is None or when >= cutoff:
            kept.append(event)
    return kept


def record_event(path: Path, event: dict[str, Any], *, now: dt.datetime,
                 retention_days: int = RETENTION_DAYS) -> None:
    """Append one event to ``path`` and enforce retention, atomically.

    Reads this agent's own file (tolerantly -- see :func:`load_events`),
    drops anything older than the retention window, appends the new event,
    and rewrites the whole (small, bounded) file in one atomic replace.
    Only this agent's own scheduler thread ever calls this for its own
    file, so there is no concurrent-writer race to guard against.
    """
    existing = load_events(path)
    kept = prune(existing, now, retention_days)
    kept.append({"schema": HISTORY_SCHEMA, **event})
    _write_events(path, kept)


def merge_chronological(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """All events across several per-agent files, oldest first."""
    merged: list[dict[str, Any]] = []
    for path in paths:
        merged.extend(load_events(path))
    return sorted(merged, key=lambda event: event.get("timestamp") or "")
