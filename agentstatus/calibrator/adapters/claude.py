"""Claude calibrator adapter: activity, minimal probe and classification."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from ..model import STATUS_ACTIVITY, STATUS_NONE, STATUS_UNRELIABLE, ActivityResult, Assessment, Observation
from ..persistence import PersistenceError, load_history

TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheCreationInputTokens",
                "cacheReadInputTokens", "totalTokens")
CCUSAGE_COMMAND = ("npx", "--yes", "ccusage@latest", "claude", "blocks", "--active", "--json")
TIMEOUT_SECONDS = 120.0
CREATE_NOISE_FLOOR = 128
CREATE_FRACTION = 0.10
COST_MULTIPLIER = 3.0
BASELINE_ALPHA = 0.3


class ClaudeProbeError(Exception):
    pass


def run_command(command: list[str], timeout: float = TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
    except OSError as exc:
        raise ClaudeProbeError(f"could not execute {command[0]}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.communicate()
        raise ClaudeProbeError(f"{command[0]} did not complete within {timeout:g}s") from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _aware(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(dt.timezone.utc) if result.tzinfo else None


def _number(container: Mapping[str, Any], key: str, integer: bool = True) -> int | Decimal:
    value = container.get(key)
    if isinstance(value, bool):
        raise ClaudeProbeError(f"invalid {key}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ClaudeProbeError(f"invalid {key}") from None
    if not number.is_finite() or number < 0 or (integer and number != number.to_integral_value()):
        raise ClaudeProbeError(f"invalid {key}")
    return int(number) if integer else number


def parse_active_block(raw: str, allow_missing: bool = False) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ClaudeProbeError(f"ccusage returned malformed JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise ClaudeProbeError("ccusage JSON has no blocks array")
    active = [b for b in data["blocks"] if isinstance(b, dict) and b.get("isActive") is True]
    if not active:
        if allow_missing:
            return None
        raise ClaudeProbeError("ccusage reported no active billing block")
    if len(active) != 1:
        raise ClaudeProbeError(f"ccusage reported {len(active)} active billing blocks")
    block = active[0]
    if not isinstance(block.get("id"), str) or not block["id"]:
        raise ClaudeProbeError("active block has invalid id")
    for key in ("startTime", "endTime"):
        if not isinstance(block.get(key), str) or _aware(block[key]) is None:
            raise ClaudeProbeError(f"active block has invalid {key}")
    counts = block.get("tokenCounts")
    if not isinstance(counts, dict):
        raise ClaudeProbeError("active block has no tokenCounts object")
    counters = {key: _number(block if key == "totalTokens" else counts, key) for key in TOKEN_FIELDS}
    counters["costUSD"] = _number(block, "costUSD", False)
    return {"id": block["id"], "startTime": block["startTime"], "counters": counters}


def _measure(command: list[str], runner, allow_missing=False):
    result = runner(command)
    if result.returncode:
        raise ClaudeProbeError(f"ccusage exited {result.returncode}")
    return parse_active_block(result.stdout, allow_missing)


class ClaudeCalibratorAdapter:
    provider = "anthropic"
    agent = "claude"
    display_name = "CLAUDE"
    measurement_schema = "claude-ccusage/v1"

    def __init__(self, projects_dir: Path, *, model: str = "sonnet",
                 prompt: str = "Reply only: OK", claude_command: list[str] | None = None,
                 ccusage_command: list[str] | None = None,
                 runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = run_command,
                 history_path: Path | None = None, startup_baseline: bool = False):
        self.projects_dir = projects_dir
        self.model = model
        self.prompt = prompt
        self.claude_command = list(claude_command or ["claude"])
        self.ccusage_command = list(ccusage_command or CCUSAGE_COMMAND)
        self.runner = runner
        # Optional read-only fallback source for activity detection: this
        # adapter's own measurement history. Ownership of the history file
        # (writing, rotation) stays entirely with the calibrator core; this
        # is the one place a provider adapter is allowed to read it back.
        self.history_path = history_path
        # Explicit, operator-set contract: a "good"-looking startup sample
        # only seeds the hot/cheap baseline when this is True. Without it,
        # the very first-ever measurement (against an empty baseline, hence
        # trivially passing the "good" thresholds) never seeds the baseline
        # on its own -- see assess().
        self.startup_baseline = startup_baseline

    def _latest_transcript_activity(self) -> dt.datetime | None:
        """Latest timestamped assistant response with non-zero model usage.

        The leading activity source: unlike file mtimes or user messages, it
        identifies a completed, billable model response. Unrelated malformed
        JSONL lines are ignored; a directory/read failure raises so the
        caller can count this source as unreliable rather than silently
        reporting "no activity".
        """
        if not self.projects_dir.is_dir():
            raise ClaudeProbeError(f"Claude projects directory unavailable: {self.projects_dir}")
        latest: dt.datetime | None = None
        try:
            for path in self.projects_dir.rglob("*.jsonl"):
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                            continue
                        entry = json.loads(line)
                        usage = (entry.get("message") or {}).get("usage") if isinstance(entry, dict) else None
                        if entry.get("type") != "assistant" or not isinstance(usage, dict):
                            continue
                        fields = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                        if any(isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool)
                               and usage[k] > 0 for k in fields):
                            stamp = _aware(entry.get("timestamp"))
                            if stamp is not None and (latest is None or stamp > latest):
                                latest = stamp
        except (OSError, json.JSONDecodeError) as exc:
            raise ClaudeProbeError(f"could not scan Claude projects: {exc}") from exc
        return latest

    def _latest_ccusage_activity(self) -> dt.datetime | None:
        result = self.runner(self.ccusage_command)
        if result.returncode:
            raise ClaudeProbeError("ccusage activity check failed")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeProbeError(f"ccusage activity check returned malformed JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
            raise ClaudeProbeError("ccusage activity JSON has no blocks array")
        timestamps = [stamp for block in data["blocks"] if isinstance(block, dict)
                     for stamp in [_aware(block.get("actualEndTime"))] if stamp is not None]
        return max(timestamps) if timestamps else None

    def _latest_probe_activity(self) -> dt.datetime | None:
        assert self.history_path is not None
        timestamps = [stamp for record in load_history(self.history_path)
                     if record.get("event") == "measurement" and record.get("valid") is True
                     for stamp in [_aware(record.get("timestamp"))] if stamp is not None]
        return max(timestamps) if timestamps else None

    def detect_activity(self) -> ActivityResult:
        candidates: list[tuple[dt.datetime, str]] = []
        failed_checks: list[str] = []
        reliable_checks = 0
        try:
            transcript = self._latest_transcript_activity()
            reliable_checks += 1
            if transcript is not None:
                candidates.append((transcript, "claude-transcript"))
        except ClaudeProbeError as exc:
            failed_checks.append(f"claude-transcript: {exc}")
        try:
            ccusage = self._latest_ccusage_activity()
            reliable_checks += 1
            if ccusage is not None:
                candidates.append((ccusage, "ccusage-actualEndTime"))
        except ClaudeProbeError as exc:
            failed_checks.append(f"ccusage-actualEndTime: {exc}")
        # Probe-log is a compatibility fallback only: it never counts toward
        # reliable_checks, and can never by itself turn an otherwise
        # unreliable check into a reliable one.
        if self.history_path is not None:
            try:
                probe = self._latest_probe_activity()
                if probe is not None:
                    candidates.append((probe, "probe-log"))
            except PersistenceError as exc:
                failed_checks.append(f"probe-log: {exc}")
        if reliable_checks == 0:
            return ActivityResult(STATUS_UNRELIABLE, reliable_checks=0, failed_checks=tuple(failed_checks))
        if not candidates:
            return ActivityResult(STATUS_NONE, reliable_checks=reliable_checks, failed_checks=tuple(failed_checks))
        at, source = max(candidates, key=lambda item: item[0])
        return ActivityResult(STATUS_ACTIVITY, at=at, reliable_checks=reliable_checks,
                              failed_checks=tuple(failed_checks), source=source)

    def probe(self) -> Observation:
        measurement_id = str(uuid.uuid4())
        try:
            before = _measure(self.ccusage_command, self.runner, allow_missing=True)
            result = self.runner(self.claude_command + ["--safe-mode",
                "--exclude-dynamic-system-prompt-sections", "--tools", "",
                "--model", self.model, "-p", self.prompt])
            if result.returncode:
                raise ClaudeProbeError(f"Claude exited {result.returncode}")
            after = _measure(self.ccusage_command, self.runner)
            assert after is not None
            if before and (before["id"], before["startTime"]) != (after["id"], after["startTime"]):
                raise ClaudeProbeError("billing block changed during probe")
            base = before["counters"] if before else {**{k: 0 for k in TOKEN_FIELDS}, "costUSD": Decimal(0)}
            delta = {key: after["counters"][key] - base[key] for key in (*TOKEN_FIELDS, "costUSD")}
            if any(value < 0 for value in delta.values()):
                raise ClaudeProbeError("one or more usage counters decreased during the probe")
            values = {"input_tokens": delta["inputTokens"], "output_tokens": delta["outputTokens"],
                      "cache_create_tokens": delta["cacheCreationInputTokens"],
                      "cache_read_tokens": delta["cacheReadInputTokens"],
                      "total_tokens": delta["totalTokens"], "cost_usd": float(delta["costUSD"]),
                      "initial_block": before is None}
            display = {"c_read": values["cache_read_tokens"],
                       "c_write": values["cache_create_tokens"],
                       "input": values["input_tokens"], "output": values["output_tokens"],
                       "total": values["total_tokens"], "cost": f"${values['cost_usd']:.4f}"}
            return Observation(True, measurement_id, values, display)
        except (ClaudeProbeError, OSError) as exc:
            return Observation(False, measurement_id, error=str(exc))

    def _classify(self, values: Mapping[str, Any], baseline: Mapping[str, Any]) -> str:
        if values.get("initial_block"):
            return "init"
        create = float(values.get("cache_create_tokens") or 0)
        total = float(values.get("total_tokens") or 0)
        expected_create = float(baseline.get("cache_create_tokens") or 0)
        expected_total = float(baseline.get("total_tokens") or total)
        threshold = max(CREATE_NOISE_FLOOR, expected_create + CREATE_FRACTION * expected_total)
        cost = float(values.get("cost_usd") or 0)
        expected_cost = baseline.get("cost_usd")
        if create > threshold or (expected_cost not in (None, 0) and create > CREATE_NOISE_FLOOR
                                  and cost > float(expected_cost) * COST_MULTIPLIER):
            return "bad"
        return "good"

    def _blend(self, baseline: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
        updated = dict(baseline)
        for key in ("cache_create_tokens", "cache_read_tokens", "total_tokens", "cost_usd"):
            observed = values.get(key)
            if observed is not None:
                prior = baseline.get(key)
                updated[key] = float(observed) if prior is None else ((1 - BASELINE_ALPHA) * float(prior) + BASELINE_ALPHA * float(observed))
        return updated

    def assess(self, observation: Observation, baseline: Mapping[str, Any],
               *, startup: bool = False) -> Assessment:
        if not observation.valid:
            return Assessment("fail", dict(baseline), update_baseline=False)
        values = observation.values
        classification = self._classify(values, baseline)
        # Only a "good" sample ever volunteers to move the baseline: bad and
        # init never do, and this is unconditional -- see model.Assessment.
        update_baseline = classification == "good"
        if startup and update_baseline and not self.startup_baseline:
            # A first-ever measurement is classified against an empty
            # baseline, so "good" is nearly automatic and proves nothing.
            # Seeding the hot/cheap baseline from it requires the operator's
            # explicit opt-in.
            update_baseline = False
        baseline_out = self._blend(baseline, values) if update_baseline else dict(baseline)
        return Assessment(classification, baseline_out, update_baseline)
