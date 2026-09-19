"""``agent-status-tui keepalive``: runs Claude/Codex/Grok keepalive pings.

All three agents run concurrently and independently (one thread per agent,
each owning its own provider/state file). Three modes:

- (no flag) an interactive foreground monitor: in-place live table.
- ``--once`` a direct live-test escape hatch (``KeepaliveAgent.ping_once()``:
  one immediate probe per agent, no wait, then exit).
- ``--service`` a quiet, non-interactive mode meant to run under systemd
  --user (see systemd/agent-status-keepalive.service.in): no table/ANSI, only
  operationally relevant lines on stdout/stderr for journald, and each
  agent's scheduler loop is supervised so an unexpected exception in one
  agent logs, backs off, and retries instead of silently going quiet or
  taking the others down with it.

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

from .core import INTERVAL_SECONDS, KeepaliveAgent
from .providers import ClaudeKeepalive, CodexKeepalive, GrokKeepalive

AGENTS: tuple[str, ...] = ("claude", "codex", "grok")
CSI = "\x1b["

# How long to back off before retrying an agent's scheduler loop after an
# unexpected exception (not a normal ping failure -- those are handled
# inside KeepaliveAgent.ping() and never raise). Deliberately short relative
# to the 3001s ping interval: this is about not crash-looping a CPU/log spin,
# not about the keepalive cadence itself.
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


def _fmt(value: str | None) -> str:
    if not value:
        return "-"
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")


def render_table(rows: dict[str, dict[str, Any]]) -> str:
    lines = [f"{'AGENT':<8} {'LAST':<10} {'NEXT':<10} {'STATUS':<8}"]
    for key in AGENTS:
        row = rows.get(key, {})
        lines.append(f"{key.upper():<8} {row.get('last', '-'):<10} {row.get('next', '-'):<10} "
                     f"{row.get('status', 'waiting'):<8}")
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
    second for the whole ~3001s wait.
    """
    if frame != last_frame:
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()
        return frame
    return last_frame


def _log_line(stream, message: str) -> None:
    stream.write(message + "\n")
    stream.flush()


def _supervised_run(agent: KeepaliveAgent, key: str,
                    on_status: Any = None, on_wait: Any = None,
                    sleeper: Any = time.sleep) -> None:
    """Run one agent's normal scheduled cadence forever, surviving an
    unexpected exception instead of silently going quiet.

    A normal ping failure (nonzero exit, subprocess error) never reaches
    here at all: it's already a handled ``PingResult(False, error)`` inside
    ``KeepaliveAgent.ping()``. This only catches something unexpected (a
    bug, a disk error, ...); when it happens, log it, wait a short backoff,
    and start this agent's scheduler again -- one agent's crash never takes
    the others down (each runs in its own thread) and never silently stops
    that agent forever either.
    """
    while True:
        try:
            agent.run(on_status=on_status, on_wait=on_wait)
            return  # only reached if max_pings is set (never true here)
        except Exception as exc:
            _log_line(sys.stderr, f"{key.upper()}: scheduler error: {exc!r}; "
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
                        help="fire one immediate ping per agent right now (no wait), then exit; "
                             "a live/diagnostic check, not one normal scheduled cycle")
    parser.add_argument("--service", action="store_true",
                        help="quiet, non-interactive mode for running under systemd --user: "
                             "no table/ANSI output, only operationally relevant lines on "
                             "stdout/stderr, runs until terminated")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    rows: dict[str, dict[str, Any]] = {key: {"status": "waiting"} for key in AGENTS}
    lock = threading.Lock()
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def on_wait_for(key: str):
        def on_wait(status: dict[str, Any]) -> None:
            with lock:
                rows[key]["next"] = _fmt(status["scheduled_at"])
                rows[key].setdefault("status", "waiting")

        return on_wait

    def on_status_for(key: str):
        def on_status(record: dict[str, Any]) -> None:
            with lock:
                rows[key]["last"] = _fmt(record["finished_at"])
                rows[key]["status"] = record["status"]

        return on_status

    agents = []
    for key in AGENTS:
        provider = _build_provider(key, args)
        state_path = args.state_dir / f"{key}.json"
        agents.append((key, KeepaliveAgent(provider, state_path)))

    if args.once:
        # A direct live-test: one immediate ping per agent, in parallel, no
        # wait at all -- never routed through the normal wait-then-ping
        # cadence in KeepaliveAgent.run().
        threads = [
            threading.Thread(target=agent.ping_once, kwargs={"on_status": on_status_for(key)}, daemon=True)
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

    if args.service:
        # Quiet and non-interactive: no table, no ANSI. Only operationally
        # relevant lines (startup, ping outcomes, stop, and any supervised
        # scheduler restart) go to stdout/stderr for journald. A provider
        # failure is already just a "fail" status inside KeepaliveAgent.ping()
        # and never stops the loop; _supervised_run additionally guards
        # against an unexpected exception silently ending one agent's
        # scheduler without ending the whole process.
        def service_status_for(key: str):
            def on_status(record: dict[str, Any]) -> None:
                if record["status"] == "ok":
                    _log_line(sys.stdout, f"{key.upper()}: ping ok")
                else:
                    _log_line(sys.stderr, f"{key.upper()}: ping failed: {record['error']}")
            return on_status

        _log_line(sys.stdout,
                  f"keepalive service starting: {', '.join(k.upper() for k in AGENTS)} "
                  f"(interval={INTERVAL_SECONDS}s)")
        threads = [
            threading.Thread(target=_supervised_run, args=(agent, key, service_status_for(key), None), daemon=True)
            for key, agent in agents
        ]
        for thread in threads:
            thread.start()
        while running and any(thread.is_alive() for thread in threads):
            time.sleep(1.0)
        _log_line(sys.stdout, "keepalive service stopping")
        return 0

    threads = [
        threading.Thread(
            target=_supervised_run,
            args=(agent, key, on_status_for(key), on_wait_for(key)),
            daemon=True,
        )
        for key, agent in agents
    ]
    for thread in threads:
        thread.start()

    interactive = sys.stdout.isatty()
    printed_lines = 0
    last_frame: str | None = None
    while running and any(thread.is_alive() for thread in threads):
        with lock:
            frame = render_table(rows)
        if interactive:
            printed_lines = _redraw(frame, printed_lines)
        else:
            last_frame = _print_if_changed(frame, last_frame)
        time.sleep(1.0)
    return 0
