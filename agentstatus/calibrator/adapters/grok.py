"""Grok calibrator adapter: weekly billing instrumentation and explicit probe."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from ...grok_billing import WEEKLY_PERIOD, billing_request, safe_close, select_bearer
from ..model import STATUS_ACTIVITY, STATUS_NONE, STATUS_UNRELIABLE, ActivityResult, Assessment, Observation

TIMEOUT_SECONDS = 120.0
COST_USD_TICKS_SCALE = Decimal(10) ** 10
# Present on local turn_completed events. Missing cache/reasoning counters
# mean "no usage of that kind" (0). costUsdTicks and timings are not assumed
# zero when absent: a missing cost is not $0.00.
REQUIRED_USAGE_INT_FIELDS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("totalTokens", "total_tokens"),
)
OPTIONAL_ZERO_USAGE_INT_FIELDS = (
    ("cachedReadTokens", "cache_read_tokens"),
    ("cacheCreationTokens", "cache_creation_tokens"),
    ("reasoningTokens", "reasoning_tokens"),
    ("modelCalls", "model_calls"),
)
OPTIONAL_OMIT_USAGE_INT_FIELDS = (
    ("apiDurationMs", "api_duration_ms"),
    ("costUsdTicks", "cost_usd_ticks"),
)


class GrokProbeError(Exception):
    pass


def run_command(command: list[str], timeout: float = TIMEOUT_SECONDS,
                *, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True, env=env)
    except OSError as exc:
        raise GrokProbeError(f"could not execute {command[0]}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.communicate()
        raise GrokProbeError(f"{command[0]} did not complete within {timeout:g}s") from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _whole_percent(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    percent = int(value)
    if percent < 0 or percent > 100:
        return None
    return percent


def _period_stamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_weekly_billing(payload: Any) -> dict[str, Any] | None:
    """Weekly creditUsagePercent + period bounds from one billing GET."""
    if not isinstance(payload, dict):
        return None
    config = payload.get("config")
    if not isinstance(config, dict):
        return None
    period = config.get("currentPeriod")
    if not isinstance(period, dict) or period.get("type") != WEEKLY_PERIOD:
        return None
    used = _whole_percent(config.get("creditUsagePercent"))
    start = _period_stamp(period.get("start"))
    end = _period_stamp(period.get("end"))
    if used is None or start is None or end is None:
        return None
    return {"used_percent": used, "period_start": start, "period_end": end}


def weekly_budget_fields(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    isolated: bool | None,
) -> dict[str, Any]:
    """Weekly meter. Delta 0 is a censored observation, not zero cost.

    ``weekly_isolated=True`` means only that no competing *locally visible*
    Grok session artefacts were observed during the probe. It is not a
    guarantee against other machines, remote-only budget use, or account
    activity that writes no local session files.
    """
    before_used = before.get("used_percent") if before else None
    after_used = after.get("used_percent") if after else None
    before_start = before.get("period_start") if before else None
    after_start = after.get("period_start") if after else None
    before_end = before.get("period_end") if before else None
    after_end = after.get("period_end") if after else None
    have_pair = before is not None and after is not None
    window_reset = have_pair and (
        before_start != after_start or before_end != after_end
    )
    same_window = have_pair and not window_reset and before_start is not None and before_end is not None
    delta = (after_used - before_used) if same_window else None
    if not have_pair:
        isolated_flag: bool | None = None
    elif window_reset or isolated is not True:
        isolated_flag = False
    else:
        isolated_flag = True
    return {
        "weekly_used_percent_before": before_used,
        "weekly_used_percent_after": after_used,
        "weekly_period_start_before": before_start,
        "weekly_period_start_after": after_start,
        "weekly_period_end_before": before_end,
        "weekly_period_end_after": after_end,
        "weekly_period_start": before_start if same_window else None,
        "weekly_period_end": before_end if same_window else None,
        "weekly_window_reset": window_reset if have_pair else None,
        "weekly_delta": delta,
        "weekly_isolated": isolated_flag,
    }


def _read_usage_int(usage: Mapping[str, Any], src: str) -> int | None:
    if src not in usage:
        return None
    raw = usage.get(src)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0 or int(raw) != raw:
        raise ValueError(src)
    return int(raw)


def parse_turn_usage(event: Any, session_id: str) -> dict[str, Any] | None:
    """Extract turn_completed usage for this session_id only.

    Required: input/output/total tokens. Optional cache/reasoning/modelCalls
    default to 0 when omitted (no usage of that kind). apiDurationMs and
    costUsdTicks are omitted when absent — missing cost is not $0.
    """
    if not isinstance(event, dict):
        return None
    params = event.get("params") if isinstance(event.get("params"), dict) else event
    if params.get("sessionId") != session_id:
        return None
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
        return None
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        values: dict[str, Any] = {}
        for src, dest in REQUIRED_USAGE_INT_FIELDS:
            raw = _read_usage_int(usage, src)
            if raw is None:
                return None
            values[dest] = raw
        for src, dest in OPTIONAL_ZERO_USAGE_INT_FIELDS:
            raw = _read_usage_int(usage, src)
            values[dest] = 0 if raw is None else raw
        for src, dest in OPTIONAL_OMIT_USAGE_INT_FIELDS:
            raw = _read_usage_int(usage, src)
            if raw is not None:
                values[dest] = raw
    except ValueError:
        return None
    if "cost_usd_ticks" in values:
        values["cost_usd"] = float(Decimal(values["cost_usd_ticks"]) / COST_USD_TICKS_SCALE)
    model_usage = usage.get("modelUsage")
    if model_usage is not None:
        values["model_usage"] = model_usage
    return values


def parse_session_turn_usage(path: Path, session_id: str) -> dict[str, Any] | None:
    latest = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        candidate = parse_turn_usage(event, session_id)
        if candidate is not None:
            latest = candidate
    return latest


def _aware_from_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None


class GrokCalibratorAdapter:
    provider = "xai"
    agent = "grok"
    display_name = "GROK"
    measurement_schema = "grok-turn-usage/v1"

    def __init__(self, grok_home: Path, *, model: str | None = None, prompt: str = "Reply only: OK",
                 grok_command: list[str] | None = None, scratch_root: Path | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = run_command,
                 clock: Callable[[], float] = time.time,
                 billing_reader: Callable[[], dict[str, Any] | None] | None = None,
                 session_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())):
        self.grok_home = grok_home
        self.model = model
        self.prompt = prompt
        self.grok_command = list(grok_command or ["grok"])
        self.scratch_root = scratch_root
        self.runner = runner
        self.clock = clock
        self.billing_reader = billing_reader or self._read_weekly_billing
        self.session_id_factory = session_id_factory

    def _status_env(self):
        return type("GrokHome", (), {"grok_home": self.grok_home})()

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GROK_HOME"] = str(self.grok_home)
        return env

    def _session_dir(self, scratch: str, session_id: str) -> Path:
        encoded = quote(str(Path(scratch)), safe="")
        return self.grok_home / "sessions" / encoded / session_id

    def detect_activity(self) -> ActivityResult:
        candidates: list[tuple[dt.datetime, str]] = []
        failures: list[str] = []
        reliable = 0
        root = self.grok_home / "sessions"
        try:
            if root.is_dir():
                for path in root.rglob("summary.json"):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                    except (OSError, json.JSONDecodeError, UnicodeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    for key in ("last_active_at", "updated_at"):
                        stamp = _aware_from_iso(data.get(key))
                        if stamp is not None:
                            candidates.append((stamp, "grok-summary"))
                reliable += 1
            else:
                reliable += 1
        except OSError as exc:
            failures.append(f"grok-summary: {type(exc).__name__}")
        try:
            active = self.grok_home / "active_sessions.json"
            if active.is_file():
                data = json.loads(active.read_text(encoding="utf-8", errors="replace"))
                if isinstance(data, list):
                    for entry in data:
                        if not isinstance(entry, dict):
                            continue
                        stamp = _aware_from_iso(entry.get("opened_at"))
                        if stamp is not None:
                            candidates.append((stamp, "grok-active-sessions"))
            reliable += 1
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            failures.append(f"grok-active-sessions: {type(exc).__name__}")
        if reliable == 0:
            return ActivityResult(STATUS_UNRELIABLE, reliable_checks=0, failed_checks=tuple(failures))
        if not candidates:
            return ActivityResult(STATUS_NONE, reliable_checks=reliable, failed_checks=tuple(failures))
        stamp, source = max(candidates, key=lambda item: item[0])
        return ActivityResult(STATUS_ACTIVITY, at=stamp, reliable_checks=reliable,
                              failed_checks=tuple(failures), source=source)

    def _read_weekly_billing(self) -> dict[str, Any] | None:
        token = select_bearer(self._status_env(), self.clock())
        if token is None:
            return None
        try:
            payload = billing_request(token)
        except Exception as exc:
            safe_close(exc)
            return None
        finally:
            token = None
        return parse_weekly_billing(payload)

    def _safe_billing(self) -> dict[str, Any] | None:
        try:
            return self.billing_reader()
        except Exception:
            return None

    def _session_file_snapshot(self) -> dict[Path, tuple[int, int]] | None:
        root = self.grok_home / "sessions"
        try:
            if not root.is_dir():
                return {}
            files: dict[Path, tuple[int, int]] = {}
            for path in root.rglob("*"):
                if path.name not in ("summary.json", "updates.jsonl"):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    return None
                files[path] = (stat.st_mtime_ns, stat.st_size)
            return files
        except OSError:
            return None

    def _probe_isolated(self, before: dict[Path, tuple[int, int]] | None,
                        session_dir: Path) -> bool:
        """True only if no competing locally visible Grok session files changed.

        This is not a guarantee against other machines, remote-only budget
        use, or account activity that writes no local session artefacts.
        """
        if before is None:
            return False
        after = self._session_file_snapshot()
        if after is None:
            return False
        session_root = session_dir.resolve() if session_dir.exists() else session_dir
        for path, fingerprint in after.items():
            if before.get(path) == fingerprint:
                continue
            try:
                relative = path.resolve().relative_to(session_root)
            except (ValueError, OSError):
                return False
            if relative.parts[:1] == ("..",):
                return False
        return True

    def probe(self) -> Observation:
        measurement_id = str(uuid.uuid4())
        session_id = self.session_id_factory()
        before_billing = self._safe_billing()
        before_files = self._session_file_snapshot()
        session_dir = Path()
        try:
            with tempfile.TemporaryDirectory(prefix="agent-status-grok-", dir=self.scratch_root) as scratch:
                session_dir = self._session_dir(scratch, session_id)
                command = list(self.grok_command)
                if self.model:
                    command.extend(["-m", self.model])
                command.extend([
                    "--cwd", scratch, "--session-id", session_id,
                    "--sandbox", "read-only",
                    "--output-format", "json", "--permission-mode", "dontAsk",
                    "--disable-web-search", "--no-subagents", "--max-turns", "1",
                    "--verbatim", "-p", self.prompt,
                ])
                result = self.runner(command, env=self._child_env())
            if result.returncode:
                raise GrokProbeError(f"Grok exited {result.returncode}")
            usage = parse_session_turn_usage(session_dir / "updates.jsonl", session_id)
            if usage is None:
                raise GrokProbeError("Grok probe returned no usable turn usage")
            after_billing = self._safe_billing()
            isolated = self._probe_isolated(before_files, session_dir)
            budget = weekly_budget_fields(before_billing, after_billing, isolated)
            cost = usage.get("cost_usd")
            display = {
                "c_read": usage["cache_read_tokens"],
                "c_write": usage["cache_creation_tokens"],
                "input": usage["input_tokens"],
                "output": usage["output_tokens"],
                "total": usage["total_tokens"],
                "cost": f"${cost:.4f}" if cost is not None else "-",
            }
            return Observation(True, measurement_id, {**usage, "session_id": session_id, **budget}, display)
        except (GrokProbeError, OSError) as exc:
            after_billing = self._safe_billing()
            isolated = self._probe_isolated(before_files, session_dir) if session_dir != Path() else False
            budget = weekly_budget_fields(before_billing, after_billing, isolated if session_dir != Path() else False)
            return Observation(False, measurement_id, {"session_id": session_id, **budget}, error=str(exc))

    def assess(self, observation: Observation, baseline: Mapping[str, Any],
               *, startup: bool = False) -> Assessment:
        if not observation.valid:
            return Assessment("fail", dict(baseline), update_baseline=False)
        return Assessment("ok", dict(baseline), update_baseline=False)
