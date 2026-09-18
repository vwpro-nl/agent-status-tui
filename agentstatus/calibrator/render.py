"""Chronological rendering driven by adapter-provided display fields."""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable


DISPLAY_COLUMNS = (
    ("c_read", "C.READ"),
    ("c_write", "C.WRITE"),
    ("input", "IN"),
    ("output", "OUT"),
    ("total", "TOTAL"),
    ("cost", "COST"),
)

_LEGACY_DISPLAY_KEYS = {
    "c_read": ("read", "cached"),
    "c_write": ("create", "write"),
}


def duration(seconds: int) -> str:
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def interval_label(record: dict[str, Any]) -> str:
    interval = record.get("interval_seconds")
    if interval is None:
        return "startup"
    # New records describe the scheduler decision that applies after this
    # row. Legacy records have no prospective movement, so preserve their
    # original retrospective rendering rather than changing history meaning.
    movement = (record.get("next_movement_seconds")
                if "next_movement_seconds" in record
                else record.get("movement_seconds"))
    if movement is None or movement == 0:
        suffix = "·" if movement == 0 else ""
    else:
        suffix = ("↑" if movement > 0 else "↓") + duration(abs(int(movement)))
    return duration(int(interval)) + suffix


def render_wait_status(display_name: str, interval_seconds: int, now: dt.datetime,
                       scheduled_at: dt.datetime) -> str:
    """Runtime wait line. Not a measurement: metrics are unknown, not reused."""
    metrics = "  ".join(f"{'-':<8}" for _ in DISPLAY_COLUMNS)
    next_text = scheduled_at.astimezone().strftime("%H:%M:%S")
    return (f"{now.astimezone().strftime('%H:%M:%S'):<8}  {display_name:<8} "
            f"{duration(int(interval_seconds)):<9} {metrics}  {next_text}")


def render_header() -> str:
    labels = "  ".join(f"{label:<8}" for _, label in DISPLAY_COLUMNS)
    return f"{'TIME':<8}  {'AGENT':<8} {'INTERVAL':<9} {labels}  NEXT"


def render_monitor_header() -> str:
    labels = "  ".join(f"{label:<8}" for _, label in DISPLAY_COLUMNS)
    return f"{'AGENT':<8} {'INTERVAL':<9} {'MODE':<8} {'CLASS':<8} {labels}  NEXT"


def render_monitor_row(display_name: str, *, interval_text: str, mode: str,
                       classification: str, values: dict[str, Any],
                       next_at: dt.datetime | None) -> str:
    """One read-only monitor row. Missing data renders as ``-``, never guessed."""
    metrics = "  ".join(f"{str(_metric(values, key)):<8}" for key, _ in DISPLAY_COLUMNS)
    next_text = next_at.astimezone().strftime("%H:%M:%S") if next_at else "-"
    return (f"{display_name:<8} {interval_text:<9} {mode:<8} {classification:<8} "
            f"{metrics}  {next_text}")


def _metric(values: dict[str, Any], key: str) -> Any:
    if key in values:
        return values[key]
    for legacy in _LEGACY_DISPLAY_KEYS.get(key, ()):
        if legacy in values:
            return values[legacy]
    return "-"


def render_record(record: dict[str, Any], display_name: str,
                  next_at: dt.datetime | None = None) -> str:
    timestamp = dt.datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    values = record.get("display") or {}
    metrics = "  ".join(f"{str(_metric(values, key)):<8}" for key, _ in DISPLAY_COLUMNS)
    next_text = next_at.astimezone().strftime("%H:%M:%S") if next_at else "--"
    line = (f"{timestamp.astimezone().strftime('%H:%M:%S'):<8}  {display_name:<8} "
            f"{interval_label(record):<9} {metrics}  {next_text}")
    if record.get("error"):
        line += "\n          Error: " + " ".join(str(record["error"]).split())[:500]
    return line


def chronological(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: r.get("timestamp", ""))
