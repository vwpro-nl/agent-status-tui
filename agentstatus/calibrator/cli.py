"""Command-line surface for the explicitly invoked calibrator."""

from __future__ import annotations

import argparse
import datetime as dt
import shlex
from pathlib import Path

from .adapters import ClaudeCalibratorAdapter
from .core import Calibrator
from .persistence import load_history, load_state
from .render import chronological, render_header, render_record


def default_root() -> Path:
    import os
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-status-tui" / "calibrator"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-status-tui calibrator")
    parser.add_argument("action", nargs="?", choices=("run", "status", "history"), default="run")
    parser.add_argument("--agent", choices=("claude",), default="claude")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--prompt", default="Reply only: OK")
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--ccusage-command", default="npx --yes ccusage@latest claude blocks --active --json")
    parser.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude/projects")
    parser.add_argument("--state-dir", type=Path, default=default_root())
    parser.add_argument("--check-interval", type=float, default=60)
    parser.add_argument("--max-measurements", type=int)
    args = parser.parse_args(argv)
    state_path = args.state_dir / "states" / f"{args.agent}.json"
    history_path = args.state_dir / "history.jsonl"
    adapter = ClaudeCalibratorAdapter(
        args.projects_dir, model=args.model, prompt=args.prompt,
        claude_command=shlex.split(args.claude_command),
        ccusage_command=shlex.split(args.ccusage_command),
    )
    if args.action == "status":
        state = load_state(state_path)
        print("No calibrator state." if state is None else
              f"{adapter.display_name} next={state['next_interval_seconds'] // 60}m progress={state['progress']}")
        return 0
    if args.action == "history":
        print(render_header(adapter.display_columns))
        for record in chronological(r for r in load_history(history_path) if r.get("event") == "measurement"):
            next_text = record.get("next_scheduled_at")
            next_at = dt.datetime.fromisoformat(next_text.replace("Z", "+00:00")) if next_text else None
            print(render_record(record, adapter.display_name, adapter.display_columns, next_at))
        return 0
    print(render_header(adapter.display_columns))
    calibrator = Calibrator(adapter, state_path, history_path, check_interval=args.check_interval)
    def emit(record):
        value = record.get("next_scheduled_at")
        next_at = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        print(render_record(record, adapter.display_name, adapter.display_columns, next_at), flush=True)
    return calibrator.run(max_measurements=args.max_measurements, emit=emit)
