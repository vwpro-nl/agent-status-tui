"""Chronological rendering driven by adapter-provided display fields."""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable


def duration(seconds: int) -> str:
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def interval_label(record: dict[str, Any]) -> str:
    interval = record.get("interval_seconds")
    if interval is None:
        return "startup"
    movement = record.get("movement_seconds")
    if movement is None or movement == 0:
        suffix = "·" if movement == 0 else ""
    else:
        suffix = ("↑" if movement > 0 else "↓") + duration(abs(int(movement)))
    return duration(int(interval)) + suffix


def render_header(columns: tuple[tuple[str, str], ...]) -> str:
    labels = "  ".join(f"{label:<8}" for _, label in columns)
    return f"{'TIME':<8}  {'AGENT':<8} {'INTERVAL':<9} {labels}  NEXT"


def render_record(record: dict[str, Any], display_name: str,
                  columns: tuple[tuple[str, str], ...], next_at: dt.datetime | None = None) -> str:
    timestamp = dt.datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    values = record.get("display") or {}
    metrics = "  ".join(f"{str(values.get(key, '-')):<8}" for key, _ in columns)
    next_text = next_at.astimezone().strftime("%H:%M:%S") if next_at else "--"
    line = (f"{timestamp.astimezone().strftime('%H:%M:%S'):<8}  {display_name:<8} "
            f"{interval_label(record):<9} {metrics}  {next_text}")
    if record.get("error"):
        line += "\n          Error: " + " ".join(str(record["error"]).split())[:500]
    return line


def chronological(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: r.get("timestamp", ""))
