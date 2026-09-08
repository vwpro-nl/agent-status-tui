"""One shared renderer.  Consumes an already-ordered ``list[AgentStatus]`` and
produces the compact table.  Pure: no I/O, no provider knowledge.

Locked normal-width structure::

                         AGENT STATUS
    agent      5h                 reset   week               reset   data
    CODEX      ████│██░░░  34%     29m     █████│███░  46%     1d10h   LIVE
    ...
               Sun 6 Sep 2026 05:40 · (1m) refresh in Ns

No blank lines, no emoji, no rules/underlines.  The title is centred over the
table's own width.  The footer reads
``<date/time> · (<interval>) refresh in <countdown>s``; in the full tier its
left edge is fixed at the 5h-meter column, so a shorter countdown only trims
the right-hand end.  Compact/narrow tiers centre it instead and, if it still
does not fit, drop the date/time first, then the parenthesised interval.
Narrower panes shorten bars and drop lower-priority columns rather than
wrapping.
"""

from __future__ import annotations

import time
import unicodedata

from .model import (
    BOLD,
    CYAN,
    DIM,
    FIVE_HOUR_MINUTES,
    GREEN,
    MUTED,
    RED,
    RESET,
    AgentStatus,
    age_text,
    bar,
    format_reset,
    time_budget,
)

# --- terminal-cell-aware string helpers (same approach as the sibling TUIs) --


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    if unicodedata.category(char) in ("Cf", "Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def visible_len(text: str) -> int:
    width = 0
    index = 0
    while index < len(text):
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "[":
            index += 2
            while index < len(text):
                char = text[index]
                index += 1
                if "@" <= char <= "~":
                    break
            continue
        width += char_width(text[index])
        index += 1
    return width


def ansi_ljust(text: str, width: int) -> str:
    return text + " " * max(0, width - visible_len(text))


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if visible_len(text) <= width:
        return text
    target = max(0, width - 1)
    result: list[str] = []
    used = 0
    index = 0
    while index < len(text):
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "[":
            start = index
            index += 2
            while index < len(text):
                char = text[index]
                index += 1
                if "@" <= char <= "~":
                    break
            result.append(text[start:index])
            continue
        char = text[index]
        cells = char_width(char)
        if used + cells > target:
            break
        result.append(char)
        used += cells
        index += 1
    return "".join(result) + "…" + RESET


def _center(text: str, width: int) -> str:
    pad = max(0, width - visible_len(text))
    return " " * (pad // 2) + text


# --- footer ---------------------------------------------------------------

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _clock(now: float) -> str:
    """``now`` -> ``Sun 6 Sep 2026 05:40`` in the local timezone.

    Built from fixed English tables rather than ``strftime`` locale names, so
    the footer stays English on any host locale.  Day has no leading zero;
    time is 24-hour, minute precision, no seconds.  Derived only from the
    passed ``now`` so frozen-clock renders stay deterministic.
    """
    t = time.localtime(now)
    return (
        f"{_WEEKDAYS[t.tm_wday]} {t.tm_mday} {_MONTHS[t.tm_mon - 1]} "
        f"{t.tm_year} {t.tm_hour:02d}:{t.tm_min:02d}"
    )


def _footer(
    clock: str,
    refresh_text: str,
    countdown: int,
    width: int,
    *,
    start_col: int | None = None,
    max_w: int | None = None,
) -> str:
    """``<date/time> · (<interval>) refresh in <countdown>s`` on one line.

    With ``start_col`` (the full table's 5h-meter left edge) the line is
    left-aligned from that fixed column: its left edge never moves when the
    countdown changes length -- only the right-hand end grows or shrinks.  It
    keeps that fixed column as long as it fits within ``max_w`` (the visible
    terminal width).

    Without ``start_col`` -- the compact/narrow tiers -- the line is centred as
    a single unit within ``width`` and, when it does not fit, drops the
    date/time first, then the parenthesised interval, keeping the live
    countdown longest.  Everything is measured on visible text.
    """
    refresh = f"({refresh_text}) refresh in {countdown}s"
    full = f"{clock} · {refresh}"
    if start_col is not None:
        ceiling = width if max_w is None else max_w
        if start_col + visible_len(full) <= ceiling:
            return " " * start_col + full
    if visible_len(full) <= width:
        return _center(full, width)
    if visible_len(refresh) <= width:
        return _center(refresh, width)
    return _center(f"refresh in {countdown}s", width)


# --- layout tiers -----------------------------------------------------------

TITLE = "AGENT STATUS"

# Width thresholds on the *content* width (terminal columns, no margin).
# Four cells is the smallest meter that can display every percentage label.
FULL_MIN = 72
MIN_BAR_WIDTH = 4

# (agent_w, reset_w, gap, block_gap) per width tier.  The 5h meter's
# left edge sits at ``agent_w + 1`` (the name column plus its trailing space);
# in the full tier the footer is aligned to exactly that column.
FULL_GEOMETRY = (10, 6, 2, 2)
COMPACT_GEOMETRY = (8, 5, 1, 1)


def _fixed_width(geometry: tuple[int, int, int, int]) -> int:
    agent_w, reset_w, gap, block_gap = geometry
    return agent_w + 1 + 2 * (gap + reset_w) + 2 * block_gap + len("data")


def _allocated_geometry(width: int, geometry: tuple[int, int, int, int]):
    """Return the fixed fields plus two meters that consume all surplus width."""
    agent_w, reset_w, gap, block_gap = geometry
    available_for_bars = width - _fixed_width(geometry)
    first_bar = available_for_bars // 2
    second_bar = available_for_bars - first_bar
    return agent_w, first_bar, second_bar, reset_w, gap, block_gap


COMPACT_FIXED_WIDTH = _fixed_width(COMPACT_GEOMETRY)
COMPACT_MIN = COMPACT_FIXED_WIDTH + 2 * MIN_BAR_WIDTH


def _pct_text(window) -> str:
    if window is None or window.used_percent is None:
        return "--"
    return f"{window.used_percent:.0f}%"


def _data_cell(status: AgentStatus) -> tuple[str, str]:
    if status.availability == "error":
        return "ERR", RED
    if status.freshness_kind == "live":
        return "LIVE", GREEN
    if status.freshness_kind in ("cache", "activity") and status.source_age is not None:
        return age_text(status.source_age), MUTED
    return "--", MUTED


# Provider/window combinations that are *known* to have no quota source, as
# opposed to data that is merely unavailable right now. The renderer places an
# aligned "n/a" / "5h00m" placeholder for these so the agent stays visually
# aligned with the others; every other ``window is None`` stays an honest "--".
UNSUPPORTED_WINDOWS = frozenset({("grok", "five_hour")})


def _is_unsupported(agent_key: str, slot: str) -> bool:
    return (agent_key, slot) in UNSUPPORTED_WINDOWS


def _window_block(
    window,
    now: float,
    bar_w: int,
    reset_w: int,
    gap: int,
    *,
    unsupported: bool = False,
) -> str:
    """``<meter>  <reset>`` -- the percentage lives inside ``<meter>``; there is
    no separate percentage field.

    * a window the provider *does not support* (``unsupported=True``, e.g. the
      Grok 5h window) renders the normal dark meter shape with a centred
      ``n/a`` and a green ``5h00m`` reset (a nominal 5h, through the shared
      ``format_reset``) -- a pure layout convention;
    * any other missing window is an honest centred ``--`` in both
      sub-columns, never a fabricated 0%.
    """
    if window is None:
        if unsupported:
            meter = bar(None, None, bar_w, label="n/a")
            reset_text = format_reset(FIVE_HOUR_MINUTES * 60)
            reset = GREEN + reset_text[:reset_w].ljust(reset_w) + RESET
            return meter + " " * gap + reset
        return DIM + "--".center(bar_w) + " " * gap + "--".center(reset_w) + RESET
    reset_text, pace_colour, elapsed = time_budget(window, now)
    meter = bar(window.used_percent, elapsed, bar_w)
    reset = pace_colour + reset_text[:reset_w].ljust(reset_w) + RESET
    return meter + " " * gap + reset


def _window_header(label: str, bar_w: int, reset_w: int, gap: int) -> str:
    # `label` is centred over its complete meter column; `reset` keeps its own
    # left alignment over the reset sub-column.
    return label.center(bar_w) + " " * gap + "reset".ljust(reset_w)


def _render_table(
    agents, now, agent_w, first_bar_w, second_bar_w, reset_w, gap, block_gap,
):
    # The whole header row is MUTED, matching the footer. Wrapping the built
    # string in colour does not change its visible width or positioning.
    header = MUTED + (
        "agent".ljust(agent_w)
        + " "
        + _window_header("5h", first_bar_w, reset_w, gap)
        + " " * block_gap
        + _window_header("week", second_bar_w, reset_w, gap)
        + " " * block_gap
        + "data"
    ) + RESET
    rows = []
    for status in agents:
        dtext, dcolour = _data_cell(status)
        rows.append(
            status.display_name[:agent_w].ljust(agent_w)
            + " "
            + _window_block(
                status.five_hour, now, first_bar_w, reset_w, gap,
                unsupported=_is_unsupported(status.key, "five_hour"),
            )
            + " " * block_gap
            + _window_block(
                status.weekly, now, second_bar_w, reset_w, gap,
                unsupported=_is_unsupported(status.key, "weekly"),
            )
            + " " * block_gap
            + dcolour
            + dtext
            + RESET
        )
    return header, rows


def _render_text(agents, now):
    header = MUTED + f"{'agent':<8} {'5h':>4}/{'wk':<4} data" + RESET
    rows = []
    for status in agents:
        dtext, dcolour = _data_cell(status)
        p5 = _pct_text(status.five_hour).rjust(4)
        pw = _pct_text(status.weekly).ljust(4)
        rows.append(
            f"{status.display_name[:8]:<8} {p5}/{pw} " + dcolour + dtext + RESET
        )
    return header, rows


def render(
    agents: list[AgentStatus],
    now: float,
    interval: float,
    next_refresh_at: float,
    width: int,
) -> str:
    content_w = max(16, width)

    if content_w >= FULL_MIN:
        header, rows = _render_table(
            agents, now, *_allocated_geometry(content_w, FULL_GEOMETRY)
        )
        five_h_col = FULL_GEOMETRY[0] + 1          # name column + its space
    elif content_w >= COMPACT_MIN:
        header, rows = _render_table(
            agents, now, *_allocated_geometry(content_w, COMPACT_GEOMETRY)
        )
        five_h_col = None                          # centred fallback below
    else:
        header, rows = _render_text(agents, now)
        five_h_col = None

    body = [header, *rows]
    table_w = max((visible_len(line) for line in body), default=len(TITLE))
    centre_w = min(content_w, max(table_w, len(TITLE)))

    if interval >= 60 and interval % 60 == 0:
        refresh_text = f"{int(interval // 60)}m"
    else:
        refresh_text = f"{interval:g}s"
    countdown = max(0, int(round(next_refresh_at - now)))
    footer = _footer(
        _clock(now), refresh_text, countdown, centre_w,
        start_col=five_h_col, max_w=content_w,
    )

    # Title in battery-status-tui's bold-cyan heading style; footer in its
    # muted secondary-information colour. The title is centred over the table's
    # own width; the full-tier footer is left-aligned to the 5h-meter column
    # (compact/narrow tiers centre it). All measured on visible text so the
    # ANSI does not shift the layout.
    lines = [
        _center(BOLD + CYAN + TITLE + RESET, centre_w),
        *body,
        MUTED + footer + RESET,
    ]
    return "\n".join(clip(line, width) for line in lines)


def render_frame(
    agents: list[AgentStatus],
    now: float,
    interval: float,
    next_refresh_at: float,
    width: int,
    height: int,
) -> str:
    """Render within the real terminal rectangle with one-cell side margins."""
    if width < 3 or height < 1:
        return ""
    inner_width = width - 2
    lines = render(agents, now, interval, next_refresh_at, inner_width).splitlines()
    if len(lines) > height:
        if height == 1:
            lines = [BOLD + CYAN + TITLE + RESET]
        else:
            lines = lines[:height - 1] + [lines[-1]]
    framed = []
    for line in lines:
        line = clip(line, inner_width)
        framed.append(" " + ansi_ljust(line, inner_width) + " ")
    return "\n".join(framed)
