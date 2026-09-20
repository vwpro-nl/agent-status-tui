"""``agent-status-tui doctor``: read-only diagnostics for installed project
components.

Currently covers the standalone keepalive systemd --user service. Follows
the same (name, status, detail) convention used by this project's sibling
reference tooling (see AGENTS.md's reference-project list): each check is a
small, pure, read-only function; the CLI layer only aggregates and prints.

Doctor never starts/stops/enables/disables a systemd unit, never pings a
provider, never mutates keepalive state, and never repairs configuration.
It only inspects what already exists on disk / is already running and
reports it. A provider being unavailable (quota exhausted, CLI missing,
etc.) is not, by itself, a Doctor finding -- Doctor checks installation and
structural coherence, not whether a provider currently has quota.

Depends on :mod:`agentstatus.keepalive` for its facts (cycle configuration,
provider list, state schema) -- one-way: keepalive itself never imports this
module, so keepalive's own standalone/zero-calibrator-dependency property is
unaffected. Never imports anything from ``agentstatus.calibrator``.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .keepalive.cli import AGENTS as KEEPALIVE_AGENTS, default_root as keepalive_state_root
from .keepalive.core import AGENT_STAGGER_SECONDS, CYCLE_SECONDS
# The scheduler's own timestamp format (KeepaliveAgent._do_ping() writes
# last_ping_finished_at via core.iso(), the exact inverse of this parser):
# reused here, not reimplemented, so Doctor's validation of that field can
# never silently drift from what the scheduler itself actually writes.
from .keepalive.core import _parse_iso as _keepalive_parse_iso
from .keepalive.history import RETENTION_DAYS as HISTORY_RETENTION_DAYS
from .keepalive.history import history_path as keepalive_history_path
from .keepalive.history import load_events_with_stats as load_keepalive_history
from .keepalive.persistence import PersistenceError, load_state
from .keepalive.providers import ClaudeKeepalive, CodexKeepalive, GrokKeepalive

UNIT_NAME = "agent-status-keepalive.service"
EXPECTED_CYCLE_SECONDS = 1800  # every wall-clock :00 and :30
EXPECTED_AGENT_STAGGER_SECONDS = 5

# (name, status, detail); status is True / False / "warn" / "unknown".
CheckResult = tuple[str, Any, str]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def systemd_user_unit_path(unit: str = UNIT_NAME) -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "systemd" / "user" / unit


def _systemctl(*args: str) -> tuple[int | None, str, str]:
    """One read-only ``systemctl --user`` query. Never enables/starts/stops
    anything -- only ``is-enabled``/``is-active`` are ever passed in.
    """
    try:
        result = subprocess.run(["systemctl", "--user", *args],
                                capture_output=True, text=True, check=False)
    except OSError as exc:
        return (None, "", str(exc))
    return (result.returncode, result.stdout.strip(), result.stderr.strip())


def check_keepalive_package_available() -> CheckResult:
    """Is the keepalive component present/importable in this checkout."""
    try:
        import agentstatus.keepalive.core  # noqa: F401
        import agentstatus.keepalive.cli  # noqa: F401
        import agentstatus.keepalive.providers  # noqa: F401
    except ImportError as exc:
        return ("keepalive package", False, f"not importable: {exc}")
    return ("keepalive package", True, str(repo_root() / "agentstatus" / "keepalive"))


def check_unit_file_installed(unit_path: Path | None = None) -> CheckResult:
    path = unit_path or systemd_user_unit_path()
    if not path.is_file():
        return (UNIT_NAME, "warn",
                f"not installed: {path} (run agentstatus/keepalive/systemd/install.sh)")
    return (UNIT_NAME, True, str(path))


def check_unit_enabled(unit_path: Path | None = None) -> CheckResult:
    path = unit_path or systemd_user_unit_path()
    if not path.is_file():
        return ("enabled", "warn", "unit not installed yet")
    code, stdout, stderr = _systemctl("is-enabled", UNIT_NAME)
    if code is None:
        return ("enabled", "unknown", f"systemctl unavailable: {stderr}")
    if stdout == "enabled":
        return ("enabled", True, stdout)
    return ("enabled", "warn", stdout or stderr or "not enabled")


def check_unit_active(unit_path: Path | None = None) -> CheckResult:
    path = unit_path or systemd_user_unit_path()
    if not path.is_file():
        return ("active", "warn", "unit not installed yet")
    code, stdout, stderr = _systemctl("is-active", UNIT_NAME)
    if code is None:
        return ("active", "unknown", f"systemctl unavailable (no user session/bus?): {stderr}")
    if stdout == "active":
        return ("active", True, stdout)
    if stdout == "failed":
        return ("active", False, f"failed -- check: journalctl --user -u {UNIT_NAME}")
    # inactive/activating/deactivating/unknown while enabled is not
    # necessarily wrong -- e.g. no user session is currently running.
    return ("active", "warn", stdout or stderr or "not active (normal if no user session is running)")


def check_executable_coherence(unit_path: Path | None = None) -> CheckResult:
    """Does the installed unit's ExecStart point at a real, executable
    agent-status-tui, actually invoked as ``keepalive --service`` (not, say,
    the plain entrypoint with no arguments -- which would start the normal
    status TUI instead) -- and is it this checkout's own entrypoint.

    Deliberately not a general systemd command-line parser: ``shlex.split``
    (a plain stdlib shell-style tokenizer) turns the ExecStart line into
    real argv tokens, and every check below is an exact-token match against
    that list -- never a substring match, so a lookalike such as
    ``notkeepalive --service-bogus`` cannot pass as ``keepalive --service``.
    Only the exact literal tokens the current
    agent-status-keepalive.service.in template always emits are checked for.
    """
    path = unit_path or systemd_user_unit_path()
    if not path.is_file():
        return ("executable coherence", "warn", "unit not installed yet")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ("executable coherence", False, f"could not read unit file: {exc}")
    line_match = re.search(r'^ExecStart=(.*)$', content, re.MULTILINE)
    if not line_match:
        return ("executable coherence", False, "no ExecStart= found in installed unit")
    exec_line = line_match.group(1)
    try:
        tokens = shlex.split(exec_line)
    except ValueError as exc:
        return ("executable coherence", False, f"could not parse ExecStart= arguments: {exc}")
    if not tokens:
        return ("executable coherence", False, "ExecStart= has no executable")
    exec_path = Path(tokens[0])
    if not exec_path.is_file():
        return ("executable coherence", False, f"ExecStart target does not exist: {exec_path}")
    if not os.access(exec_path, os.X_OK):
        return ("executable coherence", False, f"ExecStart target is not executable: {exec_path}")
    # The rest of the tokens (everything after the executable) is the
    # actual invocation -- without "keepalive --service" this unit would
    # run something else entirely (e.g. the interactive status TUI) under
    # systemd, which is a real functional break, not a path problem.
    arguments = tokens[1:]
    if "keepalive" not in arguments or "--service" not in arguments:
        return ("executable coherence", False,
                f"ExecStart does not invoke 'keepalive --service': {exec_line!r}")
    missing_flags = [flag for flag in ("--claude-command", "--codex-command", "--grok-command")
                     if flag not in arguments]
    if missing_flags:
        return ("executable coherence", "warn",
                f"provider command flags missing from ExecStart ({', '.join(missing_flags)}); "
                f"those providers fall back to bare command names on systemd's own PATH")
    current = repo_root() / "agent-status-tui"
    try:
        same = exec_path.resolve() == current.resolve()
    except OSError:
        same = False
    if not same:
        return ("executable coherence", "warn",
                f"installed unit points to a different checkout: {exec_path} (this checkout: {current})")
    return ("executable coherence", True, str(exec_path))


def check_cycle_configuration() -> CheckResult:
    """The fixed wall-clock cycle (:00/:30) and per-agent stagger -- the
    production cadence since the 3001s floating-interval model was retired
    (see docs/calibrator-research.md and STATUS.md for that history).
    """
    if CYCLE_SECONDS != EXPECTED_CYCLE_SECONDS:
        return ("cycle", False,
                f"configured as {CYCLE_SECONDS}s, expected exactly {EXPECTED_CYCLE_SECONDS}s (:00/:30)")
    if AGENT_STAGGER_SECONDS != EXPECTED_AGENT_STAGGER_SECONDS:
        return ("cycle", False,
                f"agent stagger configured as {AGENT_STAGGER_SECONDS}s, "
                f"expected exactly {EXPECTED_AGENT_STAGGER_SECONDS}s")
    return ("cycle", True, f"every :00/:30, {AGENT_STAGGER_SECONDS}s stagger per agent")


def check_providers_present() -> CheckResult:
    expected = ("claude", "codex", "grok")
    if tuple(KEEPALIVE_AGENTS) != expected:
        return ("providers", False, f"AGENTS={KEEPALIVE_AGENTS!r}, expected {expected!r}")
    mismatched = [key for cls, key in
                 ((ClaudeKeepalive, "claude"), (CodexKeepalive, "codex"), (GrokKeepalive, "grok"))
                 if getattr(cls, "key", None) != key]
    if mismatched:
        return ("providers", False, f"provider class key mismatch for: {', '.join(mismatched)}")
    return ("providers", True, ", ".join(agent.upper() for agent in KEEPALIVE_AGENTS))


def check_provider_state(agent_key: str, state_dir: Path | None = None) -> CheckResult:
    """Structural readability only. A provider's own last ping outcome
    (e.g. a quota-exhausted failure) is never a Doctor finding -- that is
    external provider availability, not installation/config coherence.
    """
    root = state_dir or keepalive_state_root()
    path = root / f"{agent_key}.json"
    if not path.is_file():
        return (agent_key, "warn", "no state yet (never pinged)")
    try:
        state = load_state(path)
    except PersistenceError as exc:
        return (agent_key, False, f"state file present but not structurally valid: {exc}")
    if state is None:
        return (agent_key, "warn", "no state yet (never pinged)")
    if state.get("agent") != agent_key:
        return (agent_key, False, f"state identity mismatch: agent={state.get('agent')!r}")
    # last_ping_finished_at is write-only from the scheduler's own
    # perspective (the fixed :00/:30 wall-clock cycle never reads it back
    # to compute anything -- see agentstatus.keepalive.core), but it is
    # still a documented part of this state file's schema and the one
    # doctor/diagnostic surface for "when did this agent last actually
    # ping". Schema + identity alone would call a malformed value here
    # "structurally valid"; this validates the one field a human or another
    # tool is actually likely to read and trust.
    last_ping_finished_at = state.get("last_ping_finished_at")
    if last_ping_finished_at is not None:
        if not isinstance(last_ping_finished_at, str):
            return (agent_key, False,
                    f"last_ping_finished_at is not a string: {last_ping_finished_at!r}")
        try:
            parsed = _keepalive_parse_iso(last_ping_finished_at)
        except ValueError as exc:
            return (agent_key, False,
                    f"last_ping_finished_at is not a valid timestamp ({exc}): {last_ping_finished_at!r}")
        # dt.datetime.fromisoformat() happily parses a timestamp with no
        # offset (e.g. "2026-09-19T03:14:03") into a naive datetime instead
        # of raising -- the scheduler works exclusively in timezone-aware
        # UTC (see keepalive.core.iso()/utc_now()), so a naive value here
        # would still be a schema violation even though nothing currently
        # re-parses it into a scheduling decision.
        if parsed.tzinfo is None:
            return (agent_key, False,
                    f"last_ping_finished_at is not timezone-aware: {last_ping_finished_at!r}")
    detail = f"last_status={state.get('last_status') or '-'} last_ping_finished_at={state.get('last_ping_finished_at') or '-'}"
    return (agent_key, True, detail)


def check_history_dir_writable(state_dir: Path | None = None) -> CheckResult:
    """Structural only: can the history directory exist and be written to.
    Never creates it -- an absent directory is simply "not yet written",
    exactly like an agent that has never pinged.
    """
    root = (state_dir or keepalive_state_root()) / "history"
    if not root.exists():
        return ("history directory", "warn", f"not created yet: {root} (created on first recorded event)")
    if not root.is_dir():
        return ("history directory", False, f"exists but is not a directory: {root}")
    if not os.access(root, os.W_OK | os.X_OK):
        return ("history directory", False, f"not writable: {root}")
    return ("history directory", True, str(root))


def check_history_file(agent_key: str, state_dir: Path | None = None,
                       now: dt.datetime | None = None) -> CheckResult:
    """One agent's history file: parseable events, and retention bounds.

    A missing file is "warn" (never pinged, or never observed yet), exactly
    like Doctor's existing keepalive-state convention. Any unparseable
    lines are reported but never treated as a hard failure -- corrupt
    history self-heals on the next successful ping (see
    ``keepalive.history.record_event``) and must never look like an
    installation problem that needs manual repair.
    """
    path = keepalive_history_path(state_dir or keepalive_state_root(), agent_key)
    if not path.is_file():
        return (agent_key, "warn", "no history yet (never pinged, or history not yet recorded)")
    events, skipped = load_keepalive_history(path)
    if not events and skipped:
        return (agent_key, "warn", f"{skipped} line(s) could not be parsed; no valid events remain")
    if not events:
        return (agent_key, "warn", "history file present but empty")
    clock = now or dt.datetime.now(dt.timezone.utc)
    oldest = min(events, key=lambda e: e.get("timestamp") or "")
    oldest_at = _keepalive_parse_iso(oldest["timestamp"]) if isinstance(oldest.get("timestamp"), str) else None
    stale = False
    if oldest_at is not None and oldest_at.tzinfo is not None:
        age_days = (clock - oldest_at).total_seconds() / 86400
        stale = age_days > HISTORY_RETENTION_DAYS + 1  # +1 day slack for cadence/jitter
    detail = f"{len(events)} event(s)"
    if skipped:
        detail += f", {skipped} unparseable line(s) skipped (self-heals on next ping)"
    if stale:
        detail += f", oldest event exceeds {HISTORY_RETENTION_DAYS}d retention -- check housekeeping"
        return (agent_key, "warn", detail)
    return (agent_key, True, detail)


def format_status(status: Any) -> str:
    if status is True:
        return "[OK]"
    if status == "warn":
        return "[WARN]"
    if status == "unknown":
        return "[UNKNOWN]"
    return "[FAIL]"


def print_results(title: str, results: list[CheckResult]) -> bool:
    print(title)
    failed = False
    for name, status, detail in results:
        print(f"  {format_status(status)} {name}: {detail}")
        if status is False:
            failed = True
    print()
    return failed


def main(argv: list[str]) -> int:
    print("agent-status-tui doctor")
    print()

    failed = False
    failed |= print_results("Keepalive component", [
        check_keepalive_package_available(),
        check_unit_file_installed(),
        check_unit_enabled(),
        check_unit_active(),
        check_executable_coherence(),
    ])
    failed |= print_results("Keepalive configuration", [
        check_cycle_configuration(),
        check_providers_present(),
    ])
    failed |= print_results("Keepalive state", [
        check_provider_state(agent) for agent in KEEPALIVE_AGENTS
    ])
    failed |= print_results("Keepalive history", [
        check_history_dir_writable(),
        *[check_history_file(agent) for agent in KEEPALIVE_AGENTS],
    ])

    if failed:
        print("Summary: problems found")
    else:
        print("Summary: all checks passed (warnings above, if any, are not failures)")
    return 1 if failed else 0
