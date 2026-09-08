"""CLI entry point and the display loop.

Two clocks, exactly as in the sibling tools:

* a ~1 second display heartbeat that only re-renders (the footer countdown and
  each window's reset countdown tick against the live clock);
* a ~60 second real data refresh that re-polls every provider.

A heartbeat redraw never calls :func:`collect`.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import time
from typing import Any

from .env import Env
from .model import CSI, RESET
from .poll import collect
from .render import render_frame


def _park_cursor(frame: str) -> str:
    """Place the cursor after bottom-row content, clear of deferred wrap."""
    rows = max(1, len(frame.splitlines()))
    columns = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
    return f"{CSI}{rows};{columns}H"


def _build_frame(agents, now, interval, next_refresh_at) -> str:
    size = shutil.get_terminal_size((80, 24))
    return render_frame(
        agents, now, interval, next_refresh_at,
        max(1, size.columns), max(1, size.lines),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-status-tui",
        description="Compact one-table status for the local Codex / Claude / Grok agents.",
    )
    parser.add_argument("--once", action="store_true", help="render once and exit")
    parser.add_argument(
        "--interval", type=float, default=60, help="data refresh interval in seconds"
    )
    args = parser.parse_args(argv)

    env = Env.resolve()
    interactive = sys.stdout.isatty() and not args.once
    interval = max(1.0, args.interval)

    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if interactive:
        sys.stdout.write(CSI + "?25l")
    try:
        now = time.time()
        agents = collect(env, now, None)
        previous = {s.key: s for s in agents}
        next_refresh_at = now + interval

        while running:
            now = time.time()
            frame = _build_frame(agents, now, args.interval, next_refresh_at)
            if interactive:
                sys.stdout.write(CSI + "2J" + CSI + "H")
            sys.stdout.write(frame)
            if interactive:
                sys.stdout.write(_park_cursor(frame))
            if not interactive:
                sys.stdout.write("\n")
            sys.stdout.flush()

            if args.once or not interactive:
                break

            tick_deadline = time.monotonic() + 1.0
            while running and time.monotonic() < tick_deadline:
                time.sleep(min(0.2, max(0.0, tick_deadline - time.monotonic())))

            if running and time.time() >= next_refresh_at:
                now = time.time()
                agents = collect(env, now, previous)
                previous = {s.key: s for s in agents} or previous
                next_refresh_at = now + interval
    finally:
        if interactive:
            sys.stdout.write(RESET + CSI + "?25h")
            sys.stdout.flush()
    return 0
