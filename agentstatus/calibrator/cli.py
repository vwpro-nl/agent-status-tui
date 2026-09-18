"""Command-line surface for the explicitly invoked calibrator."""

from __future__ import annotations

import argparse
import datetime as dt
import shlex
from pathlib import Path

from .adapters import ClaudeCalibratorAdapter, CodexCalibratorAdapter, GrokCalibratorAdapter
from .core import Calibrator
from .persistence import load_history, load_state
from .render import chronological, render_header, render_record


def default_root() -> Path:
    import os
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-status-tui" / "calibrator"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-status-tui calibrator")
    parser.add_argument("action", nargs="?", choices=("run", "status", "history"), default="run")
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
    args = parser.parse_args(argv)
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
    return calibrator.run(max_measurements=args.max_measurements, emit=emit)
