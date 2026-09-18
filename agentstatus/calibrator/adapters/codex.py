"""Codex calibrator adapter using passive activity evidence and explicit exec."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from ..model import STATUS_ACTIVITY, STATUS_NONE, STATUS_UNRELIABLE, ActivityResult, Assessment, Observation

TIMEOUT_SECONDS = 120.0
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "total_tokens")


class CodexProbeError(Exception):
    pass


def run_command(command: list[str], timeout: float = TIMEOUT_SECONDS,
                *, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one bounded command and kill its complete process group on timeout."""
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True, env=env)
    except OSError as exc:
        raise CodexProbeError(f"could not execute {command[0]}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.communicate()
        raise CodexProbeError(f"{command[0]} did not complete within {timeout:g}s") from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None
    return None


def _token_usage(value: Any) -> dict[str, int] | None:
    """Find a complete token-usage object in one Codex JSON event."""
    if isinstance(value, dict):
        if all(key in value for key in ("input_tokens", "output_tokens")):
            result: dict[str, int] = {}
            for key in TOKEN_KEYS:
                raw = value.get(key)
                if raw is None and key in ("cached_input_tokens", "cache_write_input_tokens"):
                    raw = 0
                if raw is None and key == "total_tokens":
                    continue
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0 or int(raw) != raw:
                    return None
                result[key] = int(raw)
            if "total_tokens" not in result:
                result["total_tokens"] = (result["input_tokens"] + result["output_tokens"])
            return result if result["total_tokens"] > 0 else None
        for child in value.values():
            found = _token_usage(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _token_usage(child)
            if found is not None:
                return found
    return None


def parse_exec_usage(stdout: str) -> dict[str, int] | None:
    """Prefer the last usable token event emitted by ``codex exec --json``."""
    latest = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        candidate = _token_usage(event)
        if candidate is not None:
            latest = candidate
    return latest


class CodexCalibratorAdapter:
    provider = "openai"
    agent = "codex"
    display_name = "CODEX"
    measurement_schema = "codex-exec-usage/v1"
    display_columns = (("cached", "CACHED"), ("write", "WRITE"),
                       ("input", "IN"), ("output", "OUT"), ("total", "TOTAL"))

    def __init__(self, codex_home: Path, *, model: str, prompt: str = "Reply only: OK",
                 codex_command: list[str] | None = None, scratch_root: Path | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = run_command,
                 clock: Callable[[], float] = time.time):
        self.codex_home = codex_home
        self.model = model
        self.prompt = prompt
        self.codex_command = list(codex_command or ["codex"])
        self.scratch_root = scratch_root
        self.runner = runner
        self.clock = clock

    def _rollout_activity(self) -> dt.datetime | None:
        root = self.codex_home / "sessions"
        try:
            root.stat()
        except FileNotFoundError:
            return None
        latest = None
        for path in root.rglob("rollout-*.jsonl"):
            stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            latest = stamp if latest is None or stamp > latest else latest
        return latest

    def _history_activity(self) -> dt.datetime | None:
        path = self.codex_home / "history.jsonl"
        try:
            path.stat()
        except FileNotFoundError:
            return None
        latest = None
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = _timestamp(entry.get("ts")) if isinstance(entry, dict) else None
                latest = stamp if stamp is not None and (latest is None or stamp > latest) else latest
        return latest

    def _sqlite_activity(self) -> dt.datetime | None:
        path = self.codex_home / "state_5.sqlite"
        try:
            path.stat()
        except FileNotFoundError:
            return None
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as database:
            database.execute("PRAGMA query_only = ON")
            row = database.execute(
                "SELECT MAX(COALESCE(updated_at_ms / 1000.0, updated_at)) FROM threads"
            ).fetchone()
        return _timestamp(row[0]) if row and row[0] is not None else None

    def detect_activity(self) -> ActivityResult:
        candidates: list[tuple[dt.datetime, str]] = []
        failures = []
        reliable = 0
        for source, check in (("codex-rollout", self._rollout_activity),
                              ("codex-history", self._history_activity),
                              ("codex-sqlite", self._sqlite_activity)):
            try:
                stamp = check()
                reliable += 1
                if stamp is not None:
                    candidates.append((stamp, source))
            except (OSError, sqlite3.Error, ValueError) as exc:
                failures.append(f"{source}: {type(exc).__name__}")
        if reliable == 0:
            return ActivityResult(STATUS_UNRELIABLE, reliable_checks=0, failed_checks=tuple(failures))
        if not candidates:
            return ActivityResult(STATUS_NONE, reliable_checks=reliable, failed_checks=tuple(failures))
        stamp, source = max(candidates, key=lambda item: item[0])
        return ActivityResult(STATUS_ACTIVITY, at=stamp, reliable_checks=reliable,
                              failed_checks=tuple(failures), source=source)

    def _rollout_snapshot(self) -> dict[Path, tuple[int, int]]:
        root = self.codex_home / "sessions"
        if not root.is_dir():
            return {}
        return {path: (stat.st_mtime_ns, stat.st_size) for path in root.rglob("rollout-*.jsonl")
                for stat in [path.stat()]}

    def _fallback_usage(self, before: Mapping[Path, tuple[int, int]], started: float,
                        scratch: str) -> dict[str, int] | None:
        after = self._rollout_snapshot()
        changed = [path for path, fingerprint in after.items() if before.get(path) != fingerprint]
        if len(changed) != 1:
            return None
        # Time proximity alone is insufficient when another Codex session can
        # be active concurrently. The changed rollout must explicitly name
        # this probe's unique scratch working directory.
        raw = changed[0].read_text(encoding="utf-8", errors="replace")
        if scratch not in raw:
            return None
        latest = None
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            stamp = _timestamp(entry.get("timestamp")) if isinstance(entry, dict) else None
            if stamp is None or stamp.timestamp() + 1 < started:
                continue
            candidate = _token_usage(entry)
            if candidate is not None:
                latest = candidate
        return latest

    def probe(self) -> Observation:
        measurement_id = str(uuid.uuid4())
        try:
            before = self._rollout_snapshot()
            started = self.clock()
            with tempfile.TemporaryDirectory(prefix="agent-status-codex-", dir=self.scratch_root) as scratch:
                command = self.codex_command + ["exec", "--sandbox", "read-only",
                    "--skip-git-repo-check", "--json", "--color", "never",
                    "-m", self.model, "-C", scratch, self.prompt]
                child_env = os.environ.copy()
                child_env["CODEX_HOME"] = str(self.codex_home)
                result = self.runner(command, env=child_env)
            if result.returncode:
                raise CodexProbeError(f"Codex exited {result.returncode}")
            usage = parse_exec_usage(result.stdout)
            evidence = "stdout"
            if usage is None:
                usage = self._fallback_usage(before, started, scratch)
                evidence = "probe-rollout"
            if usage is None:
                raise CodexProbeError("Codex probe returned no usable token usage")
            display = {"cached": usage["cached_input_tokens"],
                       "write": usage["cache_write_input_tokens"],
                       "input": usage["input_tokens"], "output": usage["output_tokens"],
                       "total": usage["total_tokens"]}
            return Observation(True, measurement_id, {**usage, "evidence": evidence}, display)
        except (CodexProbeError, OSError, sqlite3.Error) as exc:
            return Observation(False, measurement_id, error=str(exc))

    def assess(self, observation: Observation, baseline: Mapping[str, Any],
               *, startup: bool = False) -> Assessment:
        if not observation.valid:
            return Assessment("fail", baseline, update_baseline=False)
        return Assessment("ok", baseline, update_baseline=False)
