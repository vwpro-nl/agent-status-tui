"""Self-contained provider pings and activity detection.

Each provider class below is entirely local to this package: one minimal,
non-interactive, read-only "Reply only: OK" CLI turn per ping (exit code 0
means ok, anything else means fail, with a short usable error), and one
filesystem mtime check per activity detection. Nothing here parses tokens,
cache, cost, quota, or billing output, and nothing here interprets a
response beyond its process exit code.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping

PROMPT = "Reply only: OK"
TIMEOUT_SECONDS = 120.0


@dataclasses.dataclass(frozen=True)
class PingResult:
    ok: bool
    error: str | None = None


def run_command(command: list[str], *, timeout: float = TIMEOUT_SECONDS,
                cwd: str | None = None, env: Mapping[str, str] | None = None) -> PingResult:
    """Run one bounded, non-interactive command; kill its whole process
    group on timeout. ``ok`` iff it exits 0; otherwise a short, useful tail
    of stderr (or stdout) explains why.
    """
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True, cwd=cwd, env=env)
    except OSError as exc:
        return PingResult(False, f"could not execute {command[0]}: {exc}")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.communicate()
        return PingResult(False, f"{command[0]} did not complete within {timeout:g}s")
    if process.returncode != 0:
        tail = " ".join((stderr or stdout or "").split())[:200]
        return PingResult(False, tail or f"{command[0]} exited {process.returncode}")
    return PingResult(True, None)


def _latest_mtime(root: Path, glob: str) -> dt.datetime | None:
    """Newest mtime among files matching ``glob`` under ``root``.

    None when the directory is missing, empty, or unreadable -- never an
    invented or guessed timestamp.
    """
    try:
        if not root.is_dir():
            return None
        latest: dt.datetime | None = None
        for path in root.glob(glob):
            if not path.is_file():
                continue
            try:
                stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            except OSError:
                continue
            if latest is None or stamp > latest:
                latest = stamp
        return latest
    except OSError:
        return None


class ClaudeKeepalive:
    key = "claude"
    display_name = "CLAUDE"

    def __init__(self, home: Path, *, model: str | None = None, prompt: str = PROMPT,
                 command: list[str] | None = None, runner: Callable[..., PingResult] = run_command):
        self.home = home
        self.model = model
        self.prompt = prompt
        self.command = list(command or ["claude"])
        self.runner = runner

    def detect_activity(self) -> dt.datetime | None:
        return _latest_mtime(self.home / "projects", "**/*.jsonl")

    def ping(self) -> PingResult:
        # No model is forced: the `claude` CLI already resolves its own
        # configured/default model when --model is omitted. Keepalive only
        # manages model choice when the caller explicitly asked it to.
        argv = self.command + ["--safe-mode", "--exclude-dynamic-system-prompt-sections", "--tools", ""]
        if self.model:
            argv += ["--model", self.model]
        argv += ["-p", self.prompt]
        return self.runner(argv)


class CodexKeepalive:
    key = "codex"
    display_name = "CODEX"

    def __init__(self, home: Path, *, model: str | None = None, prompt: str = PROMPT,
                 command: list[str] | None = None, scratch_root: Path | None = None,
                 runner: Callable[..., PingResult] = run_command):
        self.home = home
        self.model = model
        self.prompt = prompt
        self.command = list(command or ["codex"])
        self.scratch_root = scratch_root
        self.runner = runner

    def detect_activity(self) -> dt.datetime | None:
        return _latest_mtime(self.home / "sessions", "**/rollout-*.jsonl")

    def ping(self) -> PingResult:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        with tempfile.TemporaryDirectory(prefix="agent-status-keepalive-codex-", dir=self.scratch_root) as scratch:
            # No model is forced: `codex exec` resolves its own configured
            # default model when -m is omitted. A fabricated placeholder
            # value here (e.g. "unused") is rejected by Codex outright --
            # "Model metadata for `unused` not found" -- so the flag is only
            # ever added when the caller explicitly asked for a model.
            argv = self.command + ["exec", "--sandbox", "read-only", "--skip-git-repo-check",
                                   "--json", "--color", "never"]
            if self.model:
                argv += ["-m", self.model]
            argv += ["-C", scratch, self.prompt]
            return self.runner(argv, env=env)


class GrokKeepalive:
    key = "grok"
    display_name = "GROK"

    def __init__(self, home: Path, *, model: str | None = None, prompt: str = PROMPT,
                 command: list[str] | None = None, scratch_root: Path | None = None,
                 runner: Callable[..., PingResult] = run_command):
        self.home = home
        self.model = model
        self.prompt = prompt
        self.command = list(command or ["grok"])
        self.scratch_root = scratch_root
        self.runner = runner

    def detect_activity(self) -> dt.datetime | None:
        return _latest_mtime(self.home / "sessions", "**/*")

    def ping(self) -> PingResult:
        env = os.environ.copy()
        env["GROK_HOME"] = str(self.home)
        with tempfile.TemporaryDirectory(prefix="agent-status-keepalive-grok-", dir=self.scratch_root) as scratch:
            argv = list(self.command)
            if self.model:
                argv += ["-m", self.model]
            argv += ["--cwd", scratch, "--sandbox", "read-only", "--output-format", "json",
                     "--permission-mode", "dontAsk", "--disable-web-search", "--no-subagents",
                     "--max-turns", "1", "-p", self.prompt]
            return self.runner(argv, env=env)
