"""Read-only, multi-agent live view over existing per-agent calibrator state.

Structurally incapable of starting a probe: it never constructs a provider
Adapter, never imports an adapters module, and only calls the read-only
``persistence``/``controller`` helpers. It reads whatever a separately
running ``calibrator run`` process has already written; it never writes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, NamedTuple

from .controller import ensure_controller
from .persistence import PersistenceError, load_history, load_state
from .render import chronological, interval_label, render_monitor_header, render_monitor_row

# The three agents this project's calibrator adapters cover today. Monitor
# does not need an Adapter instance at all -- only the key each adapter
# stores its state/history under, and a display label.
AGENTS: tuple[tuple[str, str], ...] = (
    ("claude", "CLAUDE"),
    ("codex", "CODEX"),
    ("grok", "GROK"),
)


class AgentRow(NamedTuple):
    display_name: str
    mode: str
    record: dict[str, Any] | None  # latest "measurement" history record, if any
    next_at: dt.datetime | None    # NEXT, live-corrected for later reschedules


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_all(history_path: Path) -> list[dict[str, Any]]:
    try:
        return load_history(history_path)
    except PersistenceError:
        return []


def _latest_measurement(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    measurements = [r for r in records if r.get("event") == "measurement"]
    if not measurements:
        return None
    return chronological(measurements)[-1]


def _live_next_at(records: list[dict[str, Any]], record: dict[str, Any] | None) -> dt.datetime | None:
    """NEXT, corrected for activity reschedules the scheduler logged after
    the latest measurement.

    The latest measurement's own ``next_scheduled_at`` is the starting
    point. Any ``activity-detected`` record timestamped strictly after that
    measurement pushes the deadline forward (see core._wait_for_interval);
    the newest such record wins. A reschedule at or before the measurement's
    own timestamp belongs to an earlier wait and is ignored. As soon as a
    newer measurement exists, this always restarts from that measurement --
    it is the newest measurement's timestamp that anchors the cutoff.
    """
    if record is None:
        return None
    next_at = _parse_iso(record.get("next_scheduled_at"))
    anchor = _parse_iso(record.get("timestamp"))
    if anchor is None:
        return next_at
    best = None
    for entry in records:
        if entry.get("event") != "activity-detected":
            continue
        when = _parse_iso(entry.get("timestamp"))
        if when is None or when <= anchor:
            continue
        scheduled = _parse_iso(entry.get("scheduled_at"))
        if scheduled is None:
            continue
        if best is None or when > best[0]:
            best = (when, scheduled)
    return best[1] if best is not None else next_at


def read_agent_row(agent_key: str, display_name: str, state_dir: Path) -> AgentRow:
    """One agent's row, read fresh from its own state/history files.

    Never touches another agent's files. A missing state file (never run
    yet) is "virgin", not an error; a corrupted/partial read (e.g. observed
    mid-write by a concurrently running calibrator) degrades to the same
    safe "no data yet" row rather than crashing the whole monitor frame.
    """
    state_path = state_dir / "states" / f"{agent_key}.json"
    history_path = state_dir / "history" / f"{agent_key}.jsonl"
    try:
        state = load_state(state_path)
    except PersistenceError:
        state = None
    if state is None:
        return AgentRow(display_name, "virgin", None, None)
    mode = ensure_controller(state.get("controller")).get("mode", "virgin")
    records = _load_all(history_path)
    record = _latest_measurement(records)
    return AgentRow(display_name, mode, record, _live_next_at(records, record))


def render_agent_row(row: AgentRow) -> str:
    record = row.record
    interval_text = interval_label(record) if record is not None else "-"
    classification = record.get("classification", "-") if record is not None else "-"
    values = (record.get("display") or {}) if record is not None else {}
    return render_monitor_row(
        row.display_name, interval_text=interval_text, mode=row.mode,
        classification=str(classification), values=values, next_at=row.next_at,
    )


def read_frame(state_dir: Path, agents: tuple[tuple[str, str], ...] = AGENTS) -> list[AgentRow]:
    return [read_agent_row(key, name, state_dir) for key, name in agents]


def render_frame(rows: list[AgentRow], now: dt.datetime) -> str:
    lines = [f"Calibrator monitor -- {now.astimezone().strftime('%H:%M:%S')} (read-only)",
             render_monitor_header()]
    lines.extend(render_agent_row(row) for row in rows)
    return "\n".join(lines)
