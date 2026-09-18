"""Codex calibrator adapter using passive activity evidence and explicit exec."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
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
RATE_LIMIT_TIMEOUT_SECONDS = 5.0
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "total_tokens")
def _meter_field_names(prefix: str) -> tuple[str, ...]:
    return (
        f"{prefix}_used_percent_before",
        f"{prefix}_used_percent_after",
        f"{prefix}_resets_at_before",
        f"{prefix}_resets_at_after",
        f"{prefix}_resets_at",
        f"{prefix}_delta",
        f"{prefix}_isolated",
        f"{prefix}_window_reset",
    )


FIVE_HOUR_FIELDS = _meter_field_names("five_hour")
WEEKLY_FIELDS = _meter_field_names("weekly")


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


def _whole_percent(value: Any) -> int | None:
    """Wire usedPercent is whole percentage points; keep that resolution."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    percent = int(value)
    if percent < 0 or percent > 100:
        return None
    return percent


def _reset_epoch(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    epoch = int(value)
    return epoch if epoch > 0 else None


def _parse_limit_window(window: Any) -> dict[str, int] | None:
    if not isinstance(window, dict):
        return None
    used = _whole_percent(window.get("usedPercent"))
    resets_at = _reset_epoch(window.get("resetsAt"))
    if used is None or resets_at is None:
        return None
    return {"used_percent": used, "resets_at": resets_at}


def parse_rate_limit_windows(payload: Any) -> dict[str, dict[str, int] | None] | None:
    """Extract 5h (primary) and weekly (secondary) windows from one rateLimits/read."""
    if not isinstance(payload, dict):
        return None
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return None
    snapshots = result.get("rateLimitsByLimitId")
    snapshot = None
    if isinstance(snapshots, dict):
        snapshot = snapshots.get("codex") or next(
            (item for item in snapshots.values() if isinstance(item, dict)), None
        )
    if snapshot is None:
        snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        return None
    primary = _parse_limit_window(snapshot.get("primary"))
    secondary = _parse_limit_window(snapshot.get("secondary"))
    if primary is None and secondary is None:
        return None
    return {"primary": primary, "secondary": secondary}


def meter_budget_fields(
    prefix: str,
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    isolated: bool | None,
) -> dict[str, Any]:
    """One independent meter. Delta 0 is a censored observation, not zero cost.

    Isolation of *activity* is shared; window-reset validity is per meter, so a
    5h reset does not invalidate a same-window weekly sample.
    """
    before_used = before.get("used_percent") if before else None
    after_used = after.get("used_percent") if after else None
    before_reset = before.get("resets_at") if before else None
    after_reset = after.get("resets_at") if after else None
    have_pair = before is not None and after is not None
    window_reset = (
        have_pair and before_reset is not None and after_reset is not None
        and before_reset != after_reset
    )
    same_window = have_pair and not window_reset and before_reset is not None
    delta = (after_used - before_used) if same_window else None
    if not have_pair:
        isolated_flag: bool | None = None
    elif window_reset or isolated is not True:
        isolated_flag = False
    else:
        isolated_flag = True
    return {
        f"{prefix}_used_percent_before": before_used,
        f"{prefix}_used_percent_after": after_used,
        f"{prefix}_resets_at_before": before_reset,
        f"{prefix}_resets_at_after": after_reset,
        f"{prefix}_resets_at": before_reset if same_window else None,
        f"{prefix}_delta": delta,
        f"{prefix}_isolated": isolated_flag,
        f"{prefix}_window_reset": window_reset if have_pair else None,
    }


def five_hour_budget_fields(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    isolated: bool | None,
) -> dict[str, Any]:
    return meter_budget_fields("five_hour", before, after, isolated)


def weekly_budget_fields(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    isolated: bool | None,
) -> dict[str, Any]:
    return meter_budget_fields("weekly", before, after, isolated)


def budget_fields(
    before: dict[str, dict[str, int] | None] | None,
    after: dict[str, dict[str, int] | None] | None,
    isolated: bool | None,
) -> dict[str, Any]:
    """Combine independent 5h and weekly meters from one before/after snapshot pair."""
    before = before or {"primary": None, "secondary": None}
    after = after or {"primary": None, "secondary": None}
    return {
        **meter_budget_fields("five_hour", before.get("primary"), after.get("primary"), isolated),
        **meter_budget_fields("weekly", before.get("secondary"), after.get("secondary"), isolated),
    }


class CodexCalibratorAdapter:
    provider = "openai"
    agent = "codex"
    display_name = "CODEX"
    measurement_schema = "codex-exec-usage/v1"

    def __init__(self, codex_home: Path, *, model: str, prompt: str = "Reply only: OK",
                 codex_command: list[str] | None = None, scratch_root: Path | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = run_command,
                 clock: Callable[[], float] = time.time,
                 rate_limit_reader: Callable[[], dict[str, dict[str, int] | None] | None] | None = None):
        self.codex_home = codex_home
        self.model = model
        self.prompt = prompt
        self.codex_command = list(codex_command or ["codex"])
        self.scratch_root = scratch_root
        self.runner = runner
        self.clock = clock
        self.rate_limit_reader = rate_limit_reader or self._read_rate_limit_windows

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

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        return env

    def _read_rate_limit_windows(self) -> dict[str, dict[str, int] | None] | None:
        """One passive account/rateLimits/read for both 5h and weekly windows."""
        command = list(self.codex_command) + ["app-server", "--stdio"]
        executable = command[0] if os.path.isabs(command[0]) else shutil.which(command[0])
        if not executable:
            return None
        command[0] = executable
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1, env=self._child_env(),
            )
        except OSError:
            return None
        if process.stdin is None or process.stdout is None:
            process.kill()
            return None

        def send(message: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        try:
            send({
                "id": 1, "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-status-tui-calibrator", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            })
            deadline = time.monotonic() + RATE_LIMIT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                raw = process.stdout.readline()
                if not raw:
                    return None
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(message, dict) and message.get("id") == 1:
                    break
            else:
                return None
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            deadline = time.monotonic() + RATE_LIMIT_TIMEOUT_SECONDS
            response = None
            while time.monotonic() < deadline:
                raw = process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(message, dict) and message.get("id") == 2:
                    response = message
                    break
            return parse_rate_limit_windows(response)
        except (OSError, json.JSONDecodeError):
            return None
        finally:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _safe_limits(self) -> dict[str, dict[str, int] | None] | None:
        try:
            return self.rate_limit_reader()
        except Exception:
            return None

    def _history_line_count(self) -> int | None:
        path = self.codex_home / "history.jsonl"
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                return sum(1 for _ in handle)
        except FileNotFoundError:
            return 0
        except OSError:
            return None

    def _sqlite_snapshot(self) -> dict[str, Any] | None:
        """Thread timestamps and cwds. None means the DB could not be read reliably."""
        path = self.codex_home / "state_5.sqlite"
        try:
            path.stat()
        except FileNotFoundError:
            return {"ts": None, "threads": []}
        except OSError:
            return None
        try:
            uri = f"file:{path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as database:
                database.execute("PRAGMA query_only = ON")
                rows = database.execute(
                    "SELECT cwd, COALESCE(updated_at_ms / 1000.0, updated_at) FROM threads"
                ).fetchall()
        except sqlite3.Error:
            return None
        threads = []
        latest = None
        for cwd, raw_ts in rows:
            ts = None
            if raw_ts is not None:
                try:
                    value = float(raw_ts)
                except (TypeError, ValueError):
                    return None
                ts = value / 1000.0 if value > 10_000_000_000 else value
            threads.append({"cwd": cwd if isinstance(cwd, str) else None, "ts": ts})
            if ts is not None and (latest is None or ts > latest):
                latest = ts
        return {"ts": latest, "threads": threads}

    def _activity_watermark(self) -> dict[str, Any] | None:
        try:
            return {
                "rollouts": self._rollout_snapshot(),
                "history_lines": self._history_line_count(),
                "sqlite": self._sqlite_snapshot(),
            }
        except OSError:
            return None

    def _sqlite_activity_is_ours(self, before_sql: dict[str, Any], after_sql: dict[str, Any],
                                 scratch: str) -> bool:
        """Attribute SQLite advances to this probe via thread cwd == scratch."""
        before_ts = before_sql.get("ts")
        after_ts = after_sql.get("ts")
        if after_ts is None:
            return True
        if before_ts is not None and after_ts <= before_ts:
            return True
        scratch_key = os.path.normpath(scratch) if scratch else ""
        new_rows = [
            row for row in after_sql.get("threads") or []
            if row.get("ts") is not None and (before_ts is None or row["ts"] > before_ts)
        ]
        if not new_rows:
            return False
        for row in new_rows:
            cwd = row.get("cwd")
            if not cwd or os.path.normpath(cwd) != scratch_key:
                return False
        return True

    def _probe_isolated(self, before_marks: dict[str, Any] | None, scratch: str) -> bool:
        """True only when no other Codex activity besides this probe is visible."""
        if before_marks is None:
            return False
        after_marks = self._activity_watermark()
        if after_marks is None:
            return False
        before_rollouts = before_marks.get("rollouts") or {}
        after_rollouts = after_marks.get("rollouts") or {}
        ours = 0
        for path, fingerprint in after_rollouts.items():
            if before_rollouts.get(path) == fingerprint:
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            if scratch not in raw:
                return False
            ours += 1
        if ours > 1:
            return False
        before_lines = before_marks.get("history_lines")
        after_lines = after_marks.get("history_lines")
        if before_lines is None or after_lines is None:
            return False
        if after_lines - before_lines > 1:
            return False
        before_sql = before_marks.get("sqlite")
        after_sql = after_marks.get("sqlite")
        if not isinstance(before_sql, dict) or not isinstance(after_sql, dict):
            return False
        if not self._sqlite_activity_is_ours(before_sql, after_sql, scratch):
            return False
        return True

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
        before_limits = self._safe_limits()
        before_marks = self._activity_watermark()
        scratch = ""
        try:
            before = self._rollout_snapshot()
            started = self.clock()
            with tempfile.TemporaryDirectory(prefix="agent-status-codex-", dir=self.scratch_root) as scratch:
                command = self.codex_command + ["exec", "--sandbox", "read-only",
                    "--skip-git-repo-check", "--json", "--color", "never",
                    "-m", self.model, "-C", scratch, self.prompt]
                result = self.runner(command, env=self._child_env())
            if result.returncode:
                raise CodexProbeError(f"Codex exited {result.returncode}")
            usage = parse_exec_usage(result.stdout)
            evidence = "stdout"
            if usage is None:
                usage = self._fallback_usage(before, started, scratch)
                evidence = "probe-rollout"
            if usage is None:
                raise CodexProbeError("Codex probe returned no usable token usage")
            after_limits = self._safe_limits()
            isolated = self._probe_isolated(before_marks, scratch)
            budget = budget_fields(before_limits, after_limits, isolated)
            display = {"c_read": usage["cached_input_tokens"],
                       "c_write": usage["cache_write_input_tokens"],
                       "input": usage["input_tokens"], "output": usage["output_tokens"],
                       "total": usage["total_tokens"], "cost": "-"}
            return Observation(True, measurement_id, {**usage, "evidence": evidence, **budget}, display)
        except (CodexProbeError, OSError, sqlite3.Error) as exc:
            after_limits = self._safe_limits()
            budget = budget_fields(before_limits, after_limits, False)
            return Observation(False, measurement_id, budget, error=str(exc))

    def assess(self, observation: Observation, baseline: Mapping[str, Any],
               *, startup: bool = False) -> Assessment:
        if not observation.valid:
            return Assessment("fail", baseline, update_baseline=False)
        return Assessment("ok", baseline, update_baseline=False)
