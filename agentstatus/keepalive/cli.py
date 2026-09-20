"""``agent-status-tui keepalive``: runs Claude/Codex/Grok keepalive pings.

Production cadence is fixed wall-clock cycles at every ``:00`` and ``:30``
(see :mod:`agentstatus.keepalive.core`), visiting agents in one fixed order
with a small stagger between them, each deciding independently whether a
ping is even needed (recent real activity means ``SKIP`` -- no ping, no
model turn). Three pinger modes:

- (no flag) an interactive foreground monitor: in-place live table.
- ``--once`` a direct live-test escape hatch (``KeepaliveAgent.ping_once()``:
  one immediate, *forced* ping per agent -- bypasses the activity-aware
  decision entirely -- no wait, then exit).
- ``--service`` a quiet, non-interactive mode meant to run under systemd
  --user (see systemd/agent-status-keepalive.service.in): no table/ANSI, only
  operationally relevant lines on stdout/stderr for journald. An unexpected
  exception from the cycle runner itself (not a normal ping/slot failure --
  those are already handled and recorded as events) logs, backs off, and
  restarts the cycle loop rather than silently going quiet.

Plus two read-only subcommands over the chronological event history every
real slot (PING or SKIP) in any of the three modes above records (see
:mod:`agentstatus.keepalive.history`): ``history`` prints it once, ``monitor``
re-prints it on a refresh loop. Neither can cause a ping: see
``monitor_main``'s docstring for why that is a structural property, not just
a convention.

All scheduling semantics live in :mod:`agentstatus.keepalive.core`, and all
provider pings/activity checks live in :mod:`agentstatus.keepalive.providers`.
This whole package is self-contained and depends on no other subsystem in
this project.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shlex
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import history as keepalive_history
from . import observe as keepalive_observe
from . import render as keepalive_render
from .core import CycleRunner, KeepaliveAgent, utc_now
from .providers import ClaudeKeepalive, CodexKeepalive, GrokKeepalive

# Fixed, canonical agent order -- unchanged from before this cycle redesign.
# Every cycle visits agents in exactly this order; changing it changes which
# agent lands on which stagger offset, so it is not changed casually.
AGENTS: tuple[str, ...] = ("claude", "codex", "grok")
CSI = "\x1b["

# How long to back off before restarting the cycle loop after an unexpected
# exception escapes CycleRunner.run() itself (not a normal per-agent ping/
# slot failure -- those are already caught inside CycleRunner.run_cycle and
# recorded as events, and never raise here). Short relative to a 1800s
# cycle: this is about not crash-looping, not about the keepalive cadence.
RESTART_BACKOFF_SECONDS = 30.0


def default_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-status-tui" / "keepalive"


def _build_provider(agent: str, args: argparse.Namespace) -> Any:
    # Keepalive never manages model choice on its own: --model is passed
    # through as-is (None when the caller didn't set it), and every provider
    # omits its -m/--model flag entirely in that case, deferring to the
    # provider CLI's own configured default.
    if agent == "codex":
        return CodexKeepalive(
            args.codex_home, model=args.model, prompt=args.prompt,
            command=shlex.split(args.codex_command), scratch_root=args.scratch_dir,
        )
    if agent == "grok":
        return GrokKeepalive(
            args.grok_home, model=args.model, prompt=args.prompt,
            command=shlex.split(args.grok_command), scratch_root=args.scratch_dir,
        )
    return ClaudeKeepalive(
        args.claude_home, model=args.model, prompt=args.prompt,
        command=shlex.split(args.claude_command),
    )


def _observe_for(key: str, args: argparse.Namespace) -> keepalive_observe.Snapshot:
    if key == "codex":
        return keepalive_observe.observe(
            "codex", codex_home=args.codex_home, codex_command=shlex.split(args.codex_command))
    if key == "grok":
        return keepalive_observe.observe("grok", grok_home=args.grok_home)
    return keepalive_observe.observe("claude", claude_home=args.claude_home)


def _window_fields(window: keepalive_observe.Window | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {"used_percent": window.used_percent, "resets_at": window.resets_at}


def _record_history(key: str, record: dict[str, Any], args: argparse.Namespace) -> None:
    """Append one chronological history event for this agent's slot.

    Called for *every* slot -- PING or SKIP alike -- and always attempts the
    provider-native observation regardless of the slot's own outcome:
    technical ping/skip status and provider-native observation are
    deliberately independent facts (see the module docstrings of
    ``observe`` and ``render``). Any failure here is logged and swallowed: a
    broken observation/history write must never crash or stall the
    keepalive cycle that just completed this agent's slot.
    """
    try:
        snapshot = _observe_for(key, args)
        event = {
            "timestamp": record["timestamp"],
            "agent": key,
            "action": record["action"],
            "last_activity": record.get("last_activity"),
            "ping_status": record["ping_status"],
            "ping_error": record["ping_error"],
            "five_hour": _window_fields(snapshot.five_hour),
            "weekly": _window_fields(snapshot.weekly),
            "observation_status": snapshot.status,
            "observation_detail": snapshot.detail,
        }
        path = keepalive_history.history_path(args.state_dir, key)
        keepalive_history.record_event(path, event, now=utc_now())
    except Exception as exc:
        _log_line(sys.stderr, f"{key.upper()}: history recording failed: {exc!r}")


def _fmt(value: str | None) -> str:
    if not value:
        return "-"
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")


def render_table(rows: dict[str, dict[str, Any]]) -> str:
    lines = [f"{'AGENT':<8} {'LAST':<10} {'NEXT':<10} {'ACTION':<8} {'STATUS':<8}"]
    for key in AGENTS:
        row = rows.get(key, {})
        lines.append(f"{key.upper():<8} {row.get('last', '-'):<10} {row.get('next', '-'):<10} "
                     f"{row.get('action', '-'):<8} {row.get('status', 'waiting'):<8}")
    return "\n".join(lines)


def _redraw(frame: str, previous_lines: int) -> int:
    """In-place terminal redraw: move the cursor back over the previous
    frame and rewrite each line, instead of printing a fresh table below the
    last one every tick. Never grows the scrollback during a normal wait.
    """
    if previous_lines:
        sys.stdout.write(f"{CSI}{previous_lines}A")
    for line in frame.split("\n"):
        sys.stdout.write(f"\r{CSI}2K{line}\n")
    sys.stdout.flush()
    return frame.count("\n") + 1


def _print_if_changed(frame: str, last_frame: str | None) -> str:
    """Non-interactive fallback: cursor movement is meaningless when stdout
    isn't a terminal (e.g. redirected to a log file), so print a new line
    only when the table's content actually changed, instead of once a
    second for the whole wait between cycles.
    """
    if frame != last_frame:
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()
        return frame
    return last_frame


def _log_line(stream, message: str) -> None:
    stream.write(message + "\n")
    stream.flush()


def _supervised_run(runner: CycleRunner, on_event: Any = None, on_wait: Any = None,
                    sleeper: Any = time.sleep) -> None:
    """Run the cycle loop forever, surviving an unexpected exception instead
    of silently going quiet.

    A normal per-agent ping/slot failure never reaches here at all: it is
    already caught and recorded as its own event inside
    ``CycleRunner.run_cycle``. This only catches something unexpected in the
    scheduling loop itself (a bug, a disk error, ...); when it happens, log
    it, wait a short backoff, and start the cycle loop again rather than
    ending the whole process.
    """
    while True:
        try:
            runner.run(on_event=on_event, on_wait=on_wait)
            return  # only reached if max_cycles is set (never true here)
        except Exception as exc:
            _log_line(sys.stderr, f"keepalive: cycle scheduler error: {exc!r}; "
                                  f"retrying in {RESTART_BACKOFF_SECONDS:g}s")
            sleeper(RESTART_BACKOFF_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-status-tui keepalive")
    parser.add_argument("--model",
                        help="explicit provider model for all three agents; omit to let each "
                             "provider CLI use its own configured default")
    parser.add_argument("--prompt", default="Reply only: OK")
    parser.add_argument("--scratch-dir", type=Path,
                        help="parent directory for temporary probe workspaces (Codex and Grok)")
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--grok-command", default="grok")
    parser.add_argument("--grok-home", type=Path, default=Path.home() / ".grok")
    parser.add_argument("--state-dir", type=Path, default=default_root())
    parser.add_argument("--once", action="store_true",
                        help="fire one immediate, forced ping per agent right now (no wait, no "
                             "activity check), then exit; a live/diagnostic check, not one "
                             "normal scheduled cycle")
    parser.add_argument("--service", action="store_true",
                        help="quiet, non-interactive mode for running under systemd --user: "
                             "no table/ANSI output, only operationally relevant lines on "
                             "stdout/stderr, runs until terminated")
    parser.add_argument("--cycle-now", action="store_true",
                        help="run exactly one normal activity-aware cycle right now (this "
                             "moment is the cycle's own boundary, not the next :00/:30), "
                             "through the same CycleRunner.run_cycle() and history/observation "
                             "path a real scheduled cycle uses, then exit; a manual production "
                             "diagnostic -- never alters the normal :00/:30 schedule")
    return parser


def build_history_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-status-tui keepalive history")
    parser.add_argument("--state-dir", type=Path, default=default_root())
    parser.add_argument("--agent", choices=AGENTS,
                        help="show only this agent's events (default: all agents, interleaved)")
    return parser


def _read_events(state_dir: Path, agent: str | None) -> list[dict[str, Any]]:
    keys = (agent,) if agent else AGENTS
    paths = [keepalive_history.history_path(state_dir, key) for key in keys]
    return keepalive_history.merge_chronological(paths)


def history_main(argv: list[str]) -> int:
    """``keepalive history``: print every retained event, oldest first, all
    agents interleaved by timestamp (or one agent's own events, with
    ``--agent``). Purely a read of existing per-agent history files -- never
    pings, never constructs a Provider.
    """
    args = build_history_parser().parse_args(argv)
    events = _read_events(args.state_dir, args.agent)
    sys.stdout.write(keepalive_render.render_events(events, show_agent=args.agent is None) + "\n")
    sys.stdout.flush()
    return 0


def build_monitor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-status-tui keepalive monitor")
    parser.add_argument("--state-dir", type=Path, default=default_root())
    parser.add_argument("--agent", choices=AGENTS)
    parser.add_argument("--refresh-interval", type=float, default=5.0,
                        help="seconds between re-reads of the per-agent history files")
    parser.add_argument("--limit", type=int, default=25,
                        help="most recent events shown per refresh")
    parser.add_argument("--once", action="store_true", help="render a single frame and exit")
    return parser


def monitor_main(argv: list[str]) -> int:
    """``keepalive monitor``: the same chronological history, live/refreshing.

    Structurally read-only and incapable of causing a ping: this function
    (and everything it calls -- :mod:`agentstatus.keepalive.history` and
    :mod:`agentstatus.keepalive.render`) never imports or references
    :mod:`agentstatus.keepalive.providers`, so no Provider/KeepaliveAgent
    can be constructed from this code path at all.
    """
    args = build_monitor_parser().parse_args(argv)
    interactive = sys.stdout.isatty() and not args.once
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while running:
        events = _read_events(args.state_dir, args.agent)[-max(1, args.limit):]
        header = f"Keepalive monitor -- {dt.datetime.now().astimezone().strftime('%H:%M:%S')} (read-only)"
        frame = header + "\n" + keepalive_render.render_events(events, show_agent=args.agent is None)
        if interactive:
            sys.stdout.write(f"{CSI}2J{CSI}H")
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()
        if args.once:
            break
        deadline = time.monotonic() + max(0.5, args.refresh_interval)
        while running and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "history":
        return history_main(argv[1:])
    if argv and argv[0] == "monitor":
        return monitor_main(argv[1:])
    args = build_parser().parse_args(argv)

    rows: dict[str, dict[str, Any]] = {key: {"status": "waiting"} for key in AGENTS}
    lock = threading.Lock()
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def on_wait(status: dict[str, Any]) -> None:
        with lock:
            for key in AGENTS:
                rows[key]["next"] = _fmt(status["scheduled_at"])
                rows[key].setdefault("status", "waiting")

    def on_event(record: dict[str, Any]) -> None:
        key = record["agent"]
        with lock:
            rows.setdefault(key, {})
            rows[key]["last"] = _fmt(record["timestamp"])
            rows[key]["action"] = record["action"].upper()
            rows[key]["status"] = record["ping_status"] or ("skip" if record["action"] == "skip" else "-")
        _record_history(key, record, args)

    agents = []
    for key in AGENTS:
        provider = _build_provider(key, args)
        state_path = args.state_dir / f"{key}.json"
        agents.append((key, KeepaliveAgent(provider, state_path)))

    if args.once:
        # A direct live-test: one immediate, forced ping per agent, in
        # parallel, no wait and no activity check at all -- never routed
        # through the normal scheduled cycle in CycleRunner.run().
        def once_status_for(key: str):
            def on_status(record: dict[str, Any]) -> None:
                with lock:
                    rows[key]["last"] = _fmt(record["timestamp"])
                    rows[key]["action"] = record["action"].upper()
                    rows[key]["status"] = record["ping_status"]
                _record_history(key, record, args)
            return on_status

        threads = [
            threading.Thread(target=agent.ping_once, kwargs={"on_status": once_status_for(key)}, daemon=True)
            for key, agent in agents
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with lock:
            sys.stdout.write(render_table(rows) + "\n")
            sys.stdout.flush()
        return 0

    runner = CycleRunner(agents)

    if args.cycle_now:
        # Manual production diagnostic: exactly one normal, activity-aware
        # cycle, routed through the exact same CycleRunner.run_cycle() the
        # real :00/:30 schedule uses -- fixed agent order, the same 0/5/10s
        # stagger, the same PING/SKIP decision, and the same on_event
        # (history/observation) callback the continuous scheduler uses
        # below. The only difference from a real cycle is the boundary
        # itself: this moment, not the next :00/:30 -- so it never alters
        # or interacts with the normal production schedule at all.
        boundary = utc_now()
        runner.run_cycle(boundary, on_event=on_event)
        with lock:
            sys.stdout.write(render_table(rows) + "\n")
            sys.stdout.flush()
        return 0

    if args.service:
        # Quiet and non-interactive: no table, no ANSI. Only operationally
        # relevant lines (startup, per-slot outcomes, stop, and any
        # supervised restart) go to stdout/stderr for journald.
        def service_event(record: dict[str, Any]) -> None:
            key = record["agent"]
            if record["action"] == "skip":
                _log_line(sys.stdout, f"{key.upper()}: skip (recent activity)")
            elif record["ping_status"] == "ok":
                _log_line(sys.stdout, f"{key.upper()}: ping ok")
            else:
                _log_line(sys.stderr, f"{key.upper()}: ping failed: {record['ping_error']}")
            _record_history(key, record, args)

        _log_line(sys.stdout,
                  f"keepalive service starting: {', '.join(k.upper() for k in AGENTS)} "
                  f"(cycle=:00/:30, stagger={runner.stagger}s)")
        thread = threading.Thread(
            target=_supervised_run, args=(runner, service_event, None), daemon=True)
        thread.start()
        while running and thread.is_alive():
            time.sleep(1.0)
        _log_line(sys.stdout, "keepalive service stopping")
        return 0

    thread = threading.Thread(
        target=_supervised_run, args=(runner, on_event, on_wait), daemon=True)
    thread.start()

    interactive = sys.stdout.isatty()
    printed_lines = 0
    last_frame: str | None = None
    while running and thread.is_alive():
        with lock:
            frame = render_table(rows)
        if interactive:
            printed_lines = _redraw(frame, printed_lines)
        else:
            last_frame = _print_if_changed(frame, last_frame)
        time.sleep(1.0)
    return 0
