"""Command-line surface for the explicitly invoked calibrator."""

from __future__ import annotations

import argparse
import datetime as dt
import shlex
import signal
import sys
import time
from pathlib import Path

from .adapters import ClaudeCalibratorAdapter, CodexCalibratorAdapter, GrokCalibratorAdapter
from .core import Calibrator
from .monitor import read_frame, render_frame
from .persistence import load_history, load_state
from .render import chronological, render_header, render_record, render_wait_status


def default_root() -> Path:
    import os
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-status-tui" / "calibrator"


def run_monitor(state_dir: Path, *, refresh_interval: float = 5.0, once: bool = False) -> int:
    """Print a read-only, all-agents frame, refreshed periodically.

    Never constructs an Adapter and never writes: each cycle only re-reads
    the per-agent state/history files a separately running calibrator
    already produced.
    """
    interactive = sys.stdout.isatty() and not once
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while running:
        now = dt.datetime.now(dt.timezone.utc)
        frame = render_frame(read_frame(state_dir), now)
        if interactive:
            sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()
        if once:
            break
        deadline = time.monotonic() + max(0.5, refresh_interval)
        while running and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-status-tui calibrator")
    parser.add_argument("action", nargs="?", choices=("run", "status", "history", "monitor"), default="run")
    parser.add_argument("--agent", choices=("claude", "codex", "grok"), default="claude")
    parser.add_argument("--model", help="provider model (required for Codex run; Claude defaults to sonnet; Grok uses CLI default when omitted)")
    parser.add_argument("--prompt", default="Reply only: OK")
    parser.add_argument("--scratch-dir", type=Path,
                        help="parent directory for temporary probe workspaces (Codex and Grok)")
    claude = parser.add_argument_group("Claude options")
    claude.add_argument("--claude-command", default="claude")
    claude.add_argument("--ccusage-command", default="npx --yes ccusage@latest claude blocks --active --json")
    claude.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude/projects")
    claude.add_argument("--startup-baseline", action="store_true",
                        help="allow a suitable Claude startup measurement to seed its baseline")
    codex = parser.add_argument_group("Codex options")
    codex.add_argument("--codex-command", default="codex")
    codex.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    grok = parser.add_argument_group("Grok options")
    grok.add_argument("--grok-command", default="grok")
    grok.add_argument("--grok-home", type=Path, default=Path.home() / ".grok")
    parser.add_argument("--state-dir", type=Path, default=default_root())
    parser.add_argument("--check-interval", type=float, default=60)
    parser.add_argument("--max-measurements", type=int)
    monitor = parser.add_argument_group("Monitor options")
    monitor.add_argument("--refresh-interval", type=float, default=5.0,
                         help="monitor: seconds between re-reads of the per-agent state/history files")
    monitor.add_argument("--once", action="store_true",
                         help="monitor: render a single frame and exit")
    args = parser.parse_args(argv)
    if args.action == "monitor":
        # Structurally read-only: no Adapter is ever constructed on this
        # path, so no probe/provider process/model turn can be reached from
        # here regardless of --agent/--model.
        return run_monitor(args.state_dir, refresh_interval=args.refresh_interval, once=args.once)
    if args.agent == "codex" and args.action == "run" and not args.model:
        parser.error("--model is required for a Codex calibrator run")
    state_path = args.state_dir / "states" / f"{args.agent}.json"
    # Per-agent history: each agent gets its own append-only file, so no
    # shared writer/lock is needed across agents. A future multi-agent view
    # merges these chronologically via persistence.load_histories().
    history_path = args.state_dir / "history" / f"{args.agent}.jsonl"
    if args.agent == "codex":
        adapter = CodexCalibratorAdapter(
            args.codex_home, model=args.model or "unused", prompt=args.prompt,
            codex_command=shlex.split(args.codex_command), scratch_root=args.scratch_dir,
        )
    elif args.agent == "grok":
        adapter = GrokCalibratorAdapter(
            args.grok_home, model=args.model, prompt=args.prompt,
            grok_command=shlex.split(args.grok_command), scratch_root=args.scratch_dir,
        )
    else:
        adapter = ClaudeCalibratorAdapter(
            args.projects_dir, model=args.model or "sonnet", prompt=args.prompt,
            claude_command=shlex.split(args.claude_command),
            ccusage_command=shlex.split(args.ccusage_command),
            history_path=history_path, startup_baseline=args.startup_baseline,
        )
    if args.action == "status":
        state = load_state(state_path)
        print("No calibrator state." if state is None else
              f"{adapter.display_name} next={state['sampling']['next_interval_seconds'] // 60}m "
              f"progress={state['sampling']['progress']}")
        return 0
    if args.action == "history":
        print(render_header())
        for record in chronological(r for r in load_history(history_path) if r.get("event") == "measurement"):
            next_text = record.get("next_scheduled_at")
            next_at = dt.datetime.fromisoformat(next_text.replace("Z", "+00:00")) if next_text else None
            print(render_record(record, adapter.display_name, next_at))
        return 0
    print(render_header(), flush=True)
    calibrator = Calibrator(adapter, state_path, history_path, check_interval=args.check_interval)
    def emit(record):
        value = record.get("next_scheduled_at")
        next_at = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        print(render_record(record, adapter.display_name, next_at), flush=True)
    wait_shown = False
    def on_wait(status):
        # At most one wait-status line per run: just enough that INTERVAL
        # and NEXT are visible immediately after start/resume, before the
        # first measurement row exists to show them. Every later wait
        # (including reschedules) is already implied by the previous
        # measurement row's own NEXT column, so it stays silent by default.
        nonlocal wait_shown
        if wait_shown:
            return
        wait_shown = True
        scheduled = dt.datetime.fromisoformat(status["scheduled_at"].replace("Z", "+00:00"))
        when = dt.datetime.fromisoformat(status["timestamp"].replace("Z", "+00:00"))
        print(render_wait_status(adapter.display_name, status["interval_seconds"],
                                 when, scheduled), flush=True)
    return calibrator.run(max_measurements=args.max_measurements, emit=emit, on_wait=on_wait)
