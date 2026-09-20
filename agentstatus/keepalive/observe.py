"""Provider-native quota/reset observation, taken once right after a ping.

Deliberately separate from :mod:`agentstatus.keepalive.providers`: a ping's
technical outcome (``PingResult.ok``/``error``) is never conflated with what
a provider's own native quota/reset surface reports afterward -- see
``KeepaliveAgent.run_slot()``'s "PING OK is not DELIVERED" contract, which
this module exists to support without touching it.

Every function here is read-only, bounded, and passive: none of them start
a model turn or a provider session, matching this project's existing
adapter rules (see AGENTS.md). Reuses the *techniques* this project has
already proven elsewhere (see docs/ for the research history behind them),
reimplemented directly and minimally here so this package stays
self-contained and depends on no other subsystem in this project:

- Codex: the passive ``account/rateLimits/read`` JSON-RPC call over a
  bounded, short-lived ``codex app-server --stdio`` subprocess -- the same
  mechanism the dashboard's Codex adapter uses, taken here as a single
  post-ping snapshot rather than a before/after pair.
- Claude: the same local, zero-network, zero-credential caches the
  dashboard's Claude adapter reads (the Omarchy usage cache, else
  ``~/.claude.json``'s ``cachedUsageUtilization``). ``~/.claude/.credentials.json``
  is never opened.
- Grok: the same single bounded weekly billing ``GET`` the dashboard's Grok
  adapter uses, authorised with the current short-lived bearer token from
  ``~/.grok/auth.json``. ``refresh_token`` is never read or used; the token
  is never logged, persisted, or surfaced in any returned text.

Every failure mode (missing CLI, timeout, malformed response, missing
credential, expired credential, network/HTTP error, missing cache) degrades
to an "unavailable" observation with a short, fixed, non-secret detail
string -- never an exception, and never a raw exception message that could
carry response/credential text.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

APP_SERVER_TIMEOUT_SECONDS = 5.0
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_BILLING_TIMEOUT_SECONDS = 4.0
GROK_BILLING_MAX_BYTES = 1_000_000
GROK_WEEKLY_PERIOD = "USAGE_PERIOD_TYPE_WEEKLY"


@dataclasses.dataclass(frozen=True)
class Window:
    """One provider-native window, or ``None`` fields meaning 'not known'."""

    used_percent: float | None
    resets_at: str | None  # ISO-8601 UTC, or None


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """One post-ping provider-native observation.

    ``status`` is ``"ok"`` (the read itself succeeded structurally -- even
    if a given window is simply not exposed) or ``"unavailable"`` (the read
    could not be performed at all). ``detail`` is always a short, fixed,
    non-secret string; never raw exception/response text.
    """

    status: str
    five_hour: Window | None
    weekly: Window | None
    detail: str | None = None


def _unavailable(detail: str) -> Snapshot:
    return Snapshot("unavailable", None, None, detail)


# --- Codex ------------------------------------------------------------------


def _whole_percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    percent = float(value)
    if percent < 0 or percent > 100:
        return None
    return percent


def _reset_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    epoch = float(value)
    if epoch <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _codex_window(raw: Any) -> Window | None:
    if not isinstance(raw, dict):
        return None
    used = _whole_percent(raw.get("usedPercent"))
    if used is None:
        return None
    return Window(used_percent=used, resets_at=_reset_iso(raw.get("resetsAt")))


def _parse_codex_rate_limits(response: Any) -> tuple[Window | None, Window | None] | None:
    if not isinstance(response, dict):
        return None
    result = response.get("result")
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
    return _codex_window(snapshot.get("primary")), _codex_window(snapshot.get("secondary"))


def observe_codex(codex_home: Path, *, codex_command: list[str] | None = None,
                  timeout: float = APP_SERVER_TIMEOUT_SECONDS) -> Snapshot:
    """One passive ``account/rateLimits/read`` over a fresh app-server.

    Never starts a model turn or writes anything. A missing/unresolvable
    ``codex`` binary, a timeout, or a malformed response all degrade to an
    "unavailable" snapshot rather than raising.
    """
    command = list(codex_command or ["codex"])
    executable = command[0] if os.path.isabs(command[0]) else shutil.which(command[0])
    if not executable:
        return _unavailable("codex executable not found")
    command = [executable] + command[1:] + ["app-server", "--stdio"]
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
        )
    except OSError:
        return _unavailable("could not start codex app-server")
    if process.stdin is None or process.stdout is None:
        process.kill()
        return _unavailable("could not start codex app-server")

    def send(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    try:
        send({
            "id": 1, "method": "initialize",
            "params": {
                "clientInfo": {"name": "agent-status-tui-keepalive", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        })
        deadline = time.monotonic() + timeout
        initialized = False
        while time.monotonic() < deadline:
            raw = process.stdout.readline()
            if not raw:
                break
            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(message, dict) and message.get("id") == 1:
                initialized = True
                break
        if not initialized:
            return _unavailable("codex app-server did not respond to initialize")

        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        deadline = time.monotonic() + timeout
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
        if response is None:
            return _unavailable("codex rate-limit read timed out")
        windows = _parse_codex_rate_limits(response)
        if windows is None:
            return _unavailable("codex rate-limit response malformed")
        five_hour, weekly = windows
        return Snapshot("ok", five_hour, weekly)
    except (OSError, json.JSONDecodeError):
        return _unavailable("codex rate-limit read failed")
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


# --- Claude -------------------------------------------------------------------


def _claude_cache_paths(claude_home: Path) -> tuple[Path, Path]:
    """Same env-var override contract as the dashboard's :class:`Env`
    (``AGENT_STATUS_CLAUDE_JSON`` / ``AGENT_STATUS_OMARCHY_CACHE``), so the
    two subsystems agree without keepalive importing ``agentstatus.env``.
    """
    home = Path.home()
    claude_json = os.environ.get("AGENT_STATUS_CLAUDE_JSON")
    omarchy = os.environ.get("AGENT_STATUS_OMARCHY_CACHE")
    return (
        Path(claude_json) if claude_json else home / ".claude.json",
        Path(omarchy) if omarchy else home / ".cache/omarchy/agent-usage/claude-limits.json",
    )


def _load_json(path: Path) -> Any | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _omarchy_window(entries: Any, label_fragment: str) -> Window | None:
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        if not isinstance(label, str) or label_fragment not in label:
            continue
        percent = entry.get("percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        resets_at = entry.get("resetsAt") if isinstance(entry.get("resetsAt"), str) else None
        return Window(used_percent=max(0.0, min(100.0, float(percent) * 100)), resets_at=resets_at)
    return None


def _claude_json_window(utilization: Any, key: str) -> Window | None:
    if not isinstance(utilization, dict):
        return None
    entry = utilization.get(key)
    if not isinstance(entry, dict):
        return None
    used = entry.get("utilization")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    resets_at = entry.get("resets_at") if isinstance(entry.get("resets_at"), str) else None
    return Window(used_percent=max(0.0, min(100.0, float(used))), resets_at=resets_at)


def observe_claude(claude_home: Path) -> Snapshot:
    """Read-only local cache read: never opens ``~/.claude/.credentials.json``,
    never makes a network call, never starts a model turn.
    """
    claude_json_path, omarchy_path = _claude_cache_paths(claude_home)

    omarchy = _load_json(omarchy_path)
    if isinstance(omarchy, dict) and isinstance(omarchy.get("fetchedAtMs"), (int, float)):
        five_hour = _omarchy_window(omarchy.get("limits"), "5-hour")
        weekly = _omarchy_window(omarchy.get("limits"), "7-day")
        if five_hour is not None or weekly is not None:
            return Snapshot("ok", five_hour, weekly)

    claude_json = _load_json(claude_json_path)
    if isinstance(claude_json, dict):
        cached = claude_json.get("cachedUsageUtilization")
        if isinstance(cached, dict) and isinstance(cached.get("fetchedAtMs"), (int, float)):
            utilization = cached.get("utilization")
            five_hour = _claude_json_window(utilization, "five_hour")
            weekly = _claude_json_window(utilization, "seven_day")
            if five_hour is not None or weekly is not None:
                return Snapshot("ok", five_hour, weekly)

    return _unavailable("no local Claude usage cache available")


# --- Grok -----------------------------------------------------------------


def _grok_select_bearer(grok_home: Path, now: float) -> str | None:
    data = _load_json(grok_home / "auth.json")
    if not isinstance(data, dict):
        return None
    best_rank = None
    best_key: str | None = None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        expires_at = _parse_expires_at(entry.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        rank = expires_at if expires_at is not None else float("-inf")
        if best_rank is None or rank > best_rank:
            best_rank, best_key = rank, key
    return best_key


def _parse_expires_at(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _grok_billing_request(token: str) -> Any:
    request = urllib.request.Request(
        GROK_BILLING_URL,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=GROK_BILLING_TIMEOUT_SECONDS) as response:
        raw = response.read(GROK_BILLING_MAX_BYTES)
    return json.loads(raw.decode("utf-8", "replace"))


def _grok_weekly_window(payload: Any) -> tuple[Window | None, str | None]:
    """Returns ``(window, reason)``: ``reason`` is only set when ``window``
    is ``None``, and distinguishes *why* -- e.g. a malformed response vs. a
    structurally weekly period whose own ``creditUsagePercent`` came back
    null, which are different, independently useful diagnostic facts (the
    period/reset metadata can be fine even when the percent itself is not
    currently being reported).

    Percentage and reset are independent facts, exactly like ``Window``
    itself already models (``None`` fields mean "not known", never zero) --
    a confirmed live case: a weekly response can carry a valid ``end`` while
    ``creditUsagePercent`` is missing/null. That must never be read as
    ``0%`` (the official Grok TUI's client-side fallback, not a proven
    server value); it stays ``used_percent=None`` while a genuinely
    reported reset is still used. Only when neither survives is there
    nothing left to report.
    """
    if not isinstance(payload, dict):
        return None, "malformed"
    config = payload.get("config")
    if not isinstance(config, dict):
        return None, "malformed"
    period = config.get("currentPeriod")
    if not isinstance(period, dict) or period.get("type") != GROK_WEEKLY_PERIOD:
        return None, "not weekly"
    used = _whole_percent(config.get("creditUsagePercent"))
    end = period.get("end")
    resets_at = end if isinstance(end, str) else None
    if used is None and resets_at is None:
        return None, "weekly period present but creditUsagePercent and reset both unusable/null"
    return Window(used_percent=used, resets_at=resets_at), None


def observe_grok(grok_home: Path) -> Snapshot:
    """One bounded weekly billing GET, current bearer token only.

    Never reads/uses ``refresh_token``; never writes ``auth.json``; the
    token and any response body are never included in the returned detail
    text (fixed strings only). Grok has no native 5h source, so
    ``five_hour`` is always ``None`` here -- never fabricated.
    """
    token = _grok_select_bearer(grok_home, time.time())
    if token is None:
        return _unavailable("grok credential unavailable or expired")
    try:
        payload = _grok_billing_request(token)
    except Exception:
        return _unavailable("grok billing request failed")
    finally:
        token = None
    weekly, reason = _grok_weekly_window(payload)
    if weekly is None:
        return _unavailable(f"grok billing response unusable: {reason}")
    return Snapshot("ok", None, weekly)


def observe(agent_key: str, *, codex_home: Path | None = None, codex_command: list[str] | None = None,
           claude_home: Path | None = None, grok_home: Path | None = None) -> Snapshot:
    """Dispatch by agent key. Any unexpected error still degrades safely --
    a broken observation must never propagate into the keepalive scheduler.
    """
    try:
        if agent_key == "codex":
            assert codex_home is not None
            return observe_codex(codex_home, codex_command=codex_command)
        if agent_key == "grok":
            assert grok_home is not None
            return observe_grok(grok_home)
        if agent_key == "claude":
            assert claude_home is not None
            return observe_claude(claude_home)
    except Exception:
        return _unavailable(f"{agent_key} observation failed unexpectedly")
    return _unavailable(f"unknown agent: {agent_key}")
