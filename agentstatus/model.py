"""Provider-neutral data model plus the small shared formatting primitives.

The renderer only ever sees these types; every provider adapter is responsible
for translating its own local evidence into an ``AgentStatus``.  No
token-accounting structures live here -- the compact dashboard does not show
them.
"""

from __future__ import annotations

import dataclasses

# --- ANSI -----------------------------------------------------------------
#
# The 256-colour text constants below match battery-status-tui's palette
# exactly (its graph.py): CYAN 38;5;81 for the bold title, YELLOW 38;5;221,
# MUTED 38;5;244 for secondary/footer text.  GREEN/ORANGE/RED/BLUE stay as
# foreground pace colours (battery has no equivalent named foreground green
# or red -- only the meter gradient below).

CSI = "\x1b["
RESET = CSI + "0m"
BOLD = CSI + "1m"
DIM = CSI + "2m"
CYAN = CSI + "38;5;81m"
BLUE = CSI + "38;5;75m"
GREEN = CSI + "38;5;114m"
YELLOW = CSI + "38;5;221m"
ORANGE = CSI + "38;5;208m"
RED = CSI + "38;5;203m"
MUTED = CSI + "38;5;244m"

# Meter palette, harmonised with battery-status-tui.  The filled-capacity
# backgrounds are taken straight from battery-status-tui's BATTERY_COLOR_STOPS
# (graph.py): the "full/healthy" green at 100%, the mid amber at 50%, and the
# red at 25% -- subdued rather than the previous bright pastels.  The empty
# background is a plain dark xterm 236.  Thresholds and semantics are
# unchanged: green < 70% used, amber < 90%, red otherwise.  A single light
# text colour keeps the percentage and the elapsed marker readable across
# every fill and the dark empty background.
BAR_FILL_OK = CSI + "48;2;20;105;50m"      # battery SoC 100% (full/healthy)
BAR_FILL_WARN = CSI + "48;2;175;110;25m"   # battery SoC 50% (mid amber)
BAR_FILL_HIGH = CSI + "48;2;155;35;30m"    # battery SoC 25% (red)
BAR_EMPTY_BG = CSI + "48;5;236m"           # dark empty-meter background
BAR_TEXT_ON_FILL = CSI + "38;5;252m"
BAR_TEXT_ON_EMPTY = CSI + "38;5;252m"

# --- nominal window durations ------------------------------------------------
# Neither provider reliably delivers a window *duration*; only a reset instant.
# These fixed nominal lengths are what both sibling tools already assume and are
# used purely to place the elapsed-time marker.  A source that does deliver a
# duration overrides them per-window.
FIVE_HOUR_MINUTES = 300
WEEK_MINUTES = 10080

# --- unstarted-window tolerance --------------------------------------------
# Some providers publish a *rolling* reset for an allowance that has not been
# touched: Codex reports an unused 5h window as ``usedPercent 0`` with
# ``resetsAt`` continually re-based to ``now + windowDuration``, so the value
# advances with the wall clock and is not a real deadline.  ``time_budget``
# recognises that shape -- exactly 0% used and a reset still a full nominal
# duration away -- and presents it as a clean slate (nominal countdown, no
# elapsed marker) rather than a fake active window.
#
# The slack absorbs the gap between the ~60s live refresh and the ~1s display
# heartbeat: between refreshes the frozen ``resets_at`` stays put while ``now``
# advances, so ``resets_at - now`` drifts up to a minute below the nominal
# duration before the next refresh re-bases it.  300s covers that cycle plus a
# late refresh and clock skew, while a genuinely active window -- one used even
# briefly -- has a reset far more than 5 minutes inside its nominal length.
WINDOW_START_SLACK_SECONDS = 300


@dataclasses.dataclass(frozen=True)
class Window:
    """One rate-limit window.  ``None`` fields mean 'not known', never zero."""

    used_percent: float | None      # 0..100
    resets_at: float | None         # epoch seconds
    nominal_minutes: int | None     # for the elapsed marker only


@dataclasses.dataclass(frozen=True)
class Detection:
    """Cheap, filesystem-only answer to 'should this agent appear?'."""

    installed: bool
    ever_used: bool
    last_activity: float | None      # epoch seconds of the most recent *user* action


@dataclasses.dataclass(frozen=True)
class AgentStatus:
    """The single row the renderer draws for one agent."""

    key: str                         # "codex" | "claude" | "grok"
    display_name: str                # "CODEX"
    five_hour: Window | None         # None -> provider exposes no 5h-equivalent
    weekly: Window | None            # None -> provider exposes no weekly-equivalent
    freshness_kind: str              # "live" | "cache" | "activity" | "none"
    source_age: float | None         # seconds; frozen between data refreshes
    last_activity: float | None      # epoch seconds; ordering key only
    availability: str                # "ok" | "no-capacity-data" | "error"
    detail: str | None = None        # short note / error text


# --- compact age --------------------------------------------------------------


def age_text(seconds: float | None) -> str:
    """Compact age: ``16s`` under a minute, then whole minutes (``3m``),
    then ``2h 5m`` / ``3d 6h``.  Never a ``3m 12s`` composite."""
    if seconds is None:
        return "--"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


# --- reset countdown + pace -------------------------------------------------


def format_reset(remaining_seconds: float) -> str:
    """Compact reset countdown with the subordinate unit always zero-padded to
    two digits: ``29m`` / ``2h03m`` / ``6d18h`` / ``now``.

    This is the single place countdown text is produced -- both real window
    resets and the Grok unsupported-5h placeholder go through it, so
    ``5h0m`` / ``2d1h`` never leak out as ``5h00m`` / ``2d01h``.
    """
    if remaining_seconds <= 0:
        return "now"
    total_minutes = int(remaining_seconds // 60)
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def time_budget(window: Window | None, now: float) -> tuple[str, str, float | None]:
    """Return ``(reset_text, pace_colour, elapsed_percent)`` for a window.

    Ported from the sibling TUIs:

    * exactly 0% used with no usable reset timestamp -> ``ready``;
    * non-zero or unknown usage with no reset -> ``unknown`` (never invented);
    * exactly 0% used with a numeric reset still a full nominal duration away
      -> the window has not started (a rolling placeholder, e.g. Codex): show
      the nominal countdown (``5h00m``) and no elapsed marker, keeping the real
      ``resets_at`` so the genuine countdown/pace resumes once usage begins;
    * otherwise a compact countdown (``29m`` / ``3h12m`` / ``1d10h`` / ``now``);
    * ``elapsed_percent`` is ``(now - window_start) / duration`` where
      ``window_start = resets_at - nominal_duration`` -- only when both a reset
      and a nominal duration are known.
    * pace colour compares used% against elapsed%.
    """
    if window is None:
        return "--", MUTED, None

    used = window.used_percent
    resets_at = window.resets_at

    if not isinstance(resets_at, (int, float)):
        if used == 0.0:
            return "ready", MUTED, None
        return "unknown", MUTED, None

    reset_text = format_reset(float(resets_at) - now)

    nominal = window.nominal_minutes
    if used is None or not isinstance(nominal, (int, float)) or nominal <= 0:
        return reset_text, MUTED, None

    duration = float(nominal) * 60

    # Unstarted window: exactly 0% used with the reset still a full nominal
    # duration in the future (a rolling placeholder, not a real deadline).
    # Show the nominal countdown and suppress the elapsed marker; the real
    # ``window.resets_at`` is left untouched, so the genuine countdown and pace
    # colour resume automatically the moment usage rises above 0.  ``None`` /
    # malformed / merely-near-zero usage is deliberately excluded.
    if (
        isinstance(used, (int, float))
        and not isinstance(used, bool)
        and float(used) == 0.0
        and abs((float(resets_at) - now) - duration) <= WINDOW_START_SLACK_SECONDS
    ):
        return format_reset(duration), MUTED, None

    started_at = float(resets_at) - duration
    elapsed = max(0.0, min(100.0, (now - started_at) / duration * 100))
    ahead = used - elapsed
    if ahead <= 0:
        colour = GREEN
    elif ahead <= 10:
        colour = YELLOW
    elif ahead <= 25:
        colour = ORANGE
    else:
        colour = RED
    return reset_text, colour, elapsed


# --- percentage-label placement inside a meter -----------------------------
#
# The elapsed marker is authoritative.  Its cell is fixed by ``elapsed_percent``
# and is *never* moved, biased or clamped to make room for the percentage text
# -- the label is what adapts:
#
#   1. the label is normally centred across the whole meter;
#   2. if the real marker cell would land on the centred label, or sit directly
#      against it with no separating blank cell, the label steps aside --
#        * marker left of the meter centre     -> label immediately right of it,
#        * marker at/right of the meter centre -> label immediately left of it,
#      always keeping exactly one blank separating cell;
#   3. if the preferred side cannot hold the whole label inside the meter the
#      other side is tried;
#   4. only when neither side fits is the label dropped entirely, leaving just
#      the marker and the fill.
#
# As soon as the marker clears the centred span the label returns to centre.
# Placement is deterministic in ``marker_index`` and the visible cell count;
# there are no tuned thresholds.

LABEL_MARKER_GAP = 1


def place_label(width: int, label: str, marker_index: int | None) -> str:
    """Return a ``width``-cell overlay row (blanks plus ``label``) positioned so
    it never collides with ``marker_index``.

    ``label`` is assumed to be plain text (one visible cell per character), which
    every percentage / ``n/a`` / ``--`` label is.  An all-blank row is returned
    only when the label genuinely cannot fit beside the correctly placed marker.
    """
    span = len(label)
    if span >= width:
        return label.center(width)
    centred = (width - span) // 2

    def row(start: int) -> str:
        return " " * start + label + " " * (width - start - span)

    if marker_index is None:
        return row(centred)

    gap = LABEL_MARKER_GAP
    if marker_index < centred - gap or marker_index > centred + span - 1 + gap:
        return row(centred)

    right_start = marker_index + 1 + gap
    left_start = marker_index - gap - span
    prefer_right = marker_index < (width - 1) / 2
    order = (right_start, left_start) if prefer_right else (left_start, right_start)
    for start in order:
        if 0 <= start and start + span <= width:
            return row(start)
    return " " * width


def bar(
    used_percent: float | None,
    elapsed_percent: float | None,
    width: int,
    *,
    label: str | None = None,
) -> str:
    """The proven meter primitive from the sibling TUIs.

    * every cell carries a background colour, so the meter is one continuous
      block: a filled-capacity background up to ``used_percent`` of the width,
      an empty background after it;
    * whenever ``elapsed_percent`` is not ``None`` the elapsed-time ``│`` marker
      occupies exactly the cell its proportional value dictates -- that cell is
      never shifted for any reason.  No marker is drawn when ``elapsed_percent``
      is ``None``;
    * the percentage (or ``label`` when given) is drawn as text that is normally
      centred *across the whole meter*, not appended as a separate field.  When
      the marker would overlap or crowd the centred label the label is displaced
      to the nearest side of the marker (see ``place_label``) so it stays
      readable; it is dropped only when no side can hold it.

    Either way the meter stays continuous and exactly ``width`` cells wide.
    Returns a string whose *visible* width is exactly ``width`` (it carries
    ANSI colour, so callers must measure with ``render.visible_len``).
    """
    width = max(8, width)
    if label is None:
        label = "--" if used_percent is None else f"{used_percent:.0f}%"

    filled_cells = (
        0.0
        if used_percent is None
        else max(0.0, min(100.0, used_percent)) * width / 100
    )

    marker_index = None
    if elapsed_percent is not None:
        elapsed = max(0.0, min(100.0, elapsed_percent))
        marker_index = round(elapsed / 100 * (width - 1))

    overlay = place_label(width, label, marker_index)

    if used_percent is None or used_percent < 70:
        fill_bg = BAR_FILL_OK
    elif used_percent < 90:
        fill_bg = BAR_FILL_WARN
    else:
        fill_bg = BAR_FILL_HIGH

    cells: list[str] = []
    for index, char in enumerate(overlay):
        filled = index < filled_cells
        background = fill_bg if filled else BAR_EMPTY_BG
        foreground = BAR_TEXT_ON_FILL if filled else BAR_TEXT_ON_EMPTY
        if marker_index is not None and index == marker_index:
            char = "│"
        cells.append(background + foreground + char)
    return "".join(cells) + RESET
