"""Chronological rendering shared by ``keepalive history`` and ``keepalive
monitor``: one row per past keepalive slot, all agents interleaved by
timestamp -- never grouped per provider (unless the caller already filtered
to one agent via ``--agent``, in which case the AGENT column itself is
dropped -- see ``show_agent``).

Renders several independent facts per event, side by side, and never
conflates them (see :mod:`agentstatus.keepalive.observe`'s module
docstring and ``KeepaliveAgent.run_slot()``'s contract):

- ``ACTION`` -- whether this slot actually pinged the provider (``PING``) or
  skipped it because of recent real activity (``SKIP``). A slot happening
  at all is not the same as a ping happening.
- ``LAST ACTIVITY`` -- the real activity timestamp the slot's decision was
  based on, or ``--`` when none was known.
- ``PING`` -- the technical outcome of the ping itself (``OK``/``FAILED``),
  or ``--`` when the slot was a ``SKIP`` (no ping was attempted at all).
  This is never rendered as, or described as, "delivered": a technically
  successful ping is not proof the provider counted it as usage.
- ``5H``/``RESET``/``WEEK``/``RESET`` -- whatever the provider's own native
  quota/reset surface reported right after this slot (PING or SKIP alike --
  see ``observe``), or ``--`` when that provider has no such source (e.g.
  Grok's 5h) or the read could not be completed. An unchanged percentage is
  not, by itself, treated or labelled as evidence of failure here -- it is
  simply what was observed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any


def format_percent(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "--"
    return f"{value:.0f}%"


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def format_reset(resets_at: Any, at: Any) -> str:
    """Compact countdown remaining at event time ``at`` (not "now" -- this
    is a historical record, so it shows what was true at the moment of the
    event, not a live-recomputed value). ``--`` when either timestamp is
    missing/unparseable.
    """
    reset_time = _parse_iso(resets_at)
    at_time = _parse_iso(at)
    if reset_time is None or at_time is None:
        return "--"
    remaining = (reset_time - at_time).total_seconds()
    if remaining <= 0:
        return "now"
    total_minutes = int(remaining // 60)
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_local_time(value: Any) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "--" if value is None else str(value)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_action(value: Any) -> str:
    text = str(value or "-").upper()
    return text


def format_ping(event: dict[str, Any]) -> str:
    if event.get("action") != "ping":
        return "--"
    status = event.get("ping_status")
    if status == "ok":
        return "OK"
    if status == "fail":
        return "FAILED"
    return "--"


# (label, width). AGENT is conditionally included -- see show_agent below.
_COLUMNS_WITH_AGENT = (
    ("TIME", 19), ("AGENT", 6), ("ACTION", 6), ("LAST ACTIVITY", 19),
    ("PING", 6), ("5H", 5), ("RESET", 6), ("WEEK", 6), ("RESET", 6),
)
_COLUMNS_WITHOUT_AGENT = tuple(c for c in _COLUMNS_WITH_AGENT if c[0] != "AGENT")

_DETAIL_INDENT = 21  # aligned under TIME's own width + 2-space separator


def _columns(show_agent: bool) -> tuple[tuple[str, int], ...]:
    return _COLUMNS_WITH_AGENT if show_agent else _COLUMNS_WITHOUT_AGENT


def render_header(show_agent: bool = True) -> str:
    return "  ".join(f"{label:<{width}}" for label, width in _columns(show_agent))


def render_event(event: dict[str, Any], show_agent: bool = True) -> str:
    timestamp = event.get("timestamp")
    five_hour = event.get("five_hour") or {}
    weekly = event.get("weekly") or {}
    fields = [format_local_time(timestamp)]
    if show_agent:
        fields.append(str(event.get("agent") or "-").upper())
    fields += [
        format_action(event.get("action")),
        format_local_time(event.get("last_activity")),
        format_ping(event),
        format_percent(five_hour.get("used_percent")),
        format_reset(five_hour.get("resets_at"), timestamp),
        format_percent(weekly.get("used_percent")),
        format_reset(weekly.get("resets_at"), timestamp),
    ]
    columns = _columns(show_agent)
    line = "  ".join(f"{text:<{width}}" for text, (_label, width) in zip(fields, columns))
    if event.get("action") == "ping" and event.get("ping_status") == "fail" and event.get("ping_error"):
        line += "\n" + " " * _DETAIL_INDENT + "ping error: " + " ".join(str(event["ping_error"]).split())[:200]
    detail = event.get("observation_detail")
    if event.get("observation_status") == "unavailable" and detail:
        line += "\n" + " " * _DETAIL_INDENT + "provider status: " + str(detail)[:200]
    return line


def render_events(events: list[dict[str, Any]], show_agent: bool = True) -> str:
    lines = [render_header(show_agent)]
    lines.extend(render_event(event, show_agent) for event in events)
    return "\n".join(lines)
