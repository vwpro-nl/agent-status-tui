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

from ..model import Activity, Assessment, Observation

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
    display_columns = (("read", "READ"), ("create", "CREATE"),
                       ("total", "TOTAL"), ("cost", "API COST"))

    def __init__(self, projects_dir: Path, *, model: str = "sonnet",
                 prompt: str = "Reply only: OK", claude_command: list[str] | None = None,
                 ccusage_command: list[str] | None = None,
                 runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = run_command):
        self.projects_dir = projects_dir
        self.model = model
        self.prompt = prompt
        self.claude_command = list(claude_command or ["claude"])
        self.ccusage_command = list(ccusage_command or CCUSAGE_COMMAND)
        self.runner = runner

    def detect_activity(self) -> Activity:
        candidates: list[tuple[dt.datetime, str]] = []
        errors = []
        try:
            if not self.projects_dir.is_dir():
                raise ClaudeProbeError(f"Claude projects directory unavailable: {self.projects_dir}")
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
                            if stamp:
                                candidates.append((stamp, "claude-transcript"))
        except (OSError, json.JSONDecodeError, ClaudeProbeError) as exc:
            errors.append(str(exc))
        try:
            result = self.runner(self.ccusage_command)
            if result.returncode:
                raise ClaudeProbeError("ccusage activity check failed")
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
                raise ClaudeProbeError("ccusage activity JSON has no blocks array")
            candidates.extend((stamp, "ccusage-actualEndTime") for block in data["blocks"]
                              if isinstance(block, dict)
                              for stamp in [_aware(block.get("actualEndTime"))] if stamp)
        except (json.JSONDecodeError, ClaudeProbeError) as exc:
            errors.append(str(exc))
        if not candidates:
            raise ClaudeProbeError("activity time cannot be established reliably: " + "; ".join(errors))
        stamp, source = max(candidates)
        return Activity(stamp, source)

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
            display = {"read": values["cache_read_tokens"], "create": values["cache_create_tokens"],
                       "total": values["total_tokens"], "cost": f"${values['cost_usd']:.4f}"}
            return Observation(True, measurement_id, values, display)
        except (ClaudeProbeError, OSError) as exc:
            return Observation(False, measurement_id, error=str(exc))

    def assess(self, observation: Observation, baseline: Mapping[str, Any]) -> Assessment:
        if not observation.valid:
            return Assessment("fail", baseline)
        values = observation.values
        classification = "init" if values.get("initial_block") else "good"
        create = float(values.get("cache_create_tokens") or 0)
        total = float(values.get("total_tokens") or 0)
        expected_create = float(baseline.get("cache_create_tokens") or 0)
        expected_total = float(baseline.get("total_tokens") or total)
        threshold = max(CREATE_NOISE_FLOOR, expected_create + CREATE_FRACTION * expected_total)
        cost = float(values.get("cost_usd") or 0)
        expected_cost = baseline.get("cost_usd")
        if classification != "init" and (create > threshold or
                (expected_cost not in (None, 0) and create > CREATE_NOISE_FLOOR
                 and cost > float(expected_cost) * COST_MULTIPLIER)):
            classification = "bad"
        updated = dict(baseline)
        for key in ("cache_create_tokens", "cache_read_tokens", "total_tokens", "cost_usd"):
            observed = values.get(key)
            if observed is not None:
                prior = baseline.get(key)
                updated[key] = float(observed) if prior is None else ((1 - BASELINE_ALPHA) * float(prior) + BASELINE_ALPHA * float(observed))
        return Assessment(classification, updated)
