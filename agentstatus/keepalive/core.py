"""Clock-aligned, activity-aware, provider-agnostic keepalive scheduler.

Replaces the earlier floating-deadline model (a fixed interval since each
agent's own last ping/activity). The production cadence is now fixed
wall-clock cycles at every ``:00`` and ``:30``, not a drifting per-agent
timer:

- ``next_boundary(now)`` -- the next ``:00``/``:30`` strictly after ``now``.
  Never additive/cumulative: every cycle recomputes its own boundary fresh
  from the real clock, so a late-running cycle can never drift the next
  one.
- Every cycle visits agents in one fixed order, each agent's slot offset by
  :data:`AGENT_STAGGER_SECONDS` from the cycle boundary (0s, 5s, 10s for a
  three-agent order) -- readable logs/history, and never more than one
  provider process started at once.
- At its own slot, one agent decides ``PING`` or ``SKIP`` purely from its
  own :meth:`KeepaliveAgent.decide` (:data:`ACTIVITY_WINDOW_SECONDS` of its
  own recent real activity -- never another agent's). A ``SKIP`` never
  touches the provider at all: no ping, no model turn, no state write --
  only a history event (see :mod:`agentstatus.keepalive.cli`) records that
  the slot happened.
- One agent's failure (ping or an unexpected exception) never prevents the
  next agent's slot in the same cycle, and never delays the next cycle's
  boundary (see :class:`CycleRunner`).

``LEGACY_INTERVAL_SECONDS`` (3001) is kept only as a documented historical
constant -- the fixed interval this project used in production before fixed
:00/:30 wall-clock cycles replaced it. Nothing in this module uses it for
scheduling any more.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .persistence import STATE_SCHEMA, load_state as _load_state, save_state
from .providers import PingResult

# Historical only -- see the module docstring and STATUS.md for why 3001s
# (not 3000s) was originally chosen. No longer read anywhere in the
# scheduling path below.
LEGACY_INTERVAL_SECONDS = 3001

# Every cycle boundary is a wall-clock :00 or :30 -- i.e. every 1800 seconds.
CYCLE_SECONDS = 1800

# Fixed per-agent offset from the cycle boundary: the first agent in the
# caller-supplied order runs at the boundary itself, the second 5s later,
# the third 10s later, and so on. Small and deliberately not "as fast as
# possible": readable logs/history, and never more than one provider
# process running at once.
AGENT_STAGGER_SECONDS = 5

# A slot's PING/SKIP decision looks back exactly this far for real activity.
# Fixed and explicit by design -- not adaptive, not provider-tuned, not a
# search: the same 1800s (one cycle) for every agent, every cycle. Real
# activity at least this recent means a keepalive ping would be redundant
# (the provider has already seen the user this cycle); older or unknown
# activity means PING.
ACTIVITY_WINDOW_SECONDS = 1800

ACTION_PING = "ping"
ACTION_SKIP = "skip"


class KeepaliveError(Exception):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise KeepaliveError("keepalive clock returned a naive datetime")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def next_boundary(now: dt.datetime) -> dt.datetime:
    """The next wall-clock ``:00`` or ``:30`` strictly after ``now``.

    Pure and stateless: never additive, never remembers a previous
    boundary. Called fresh at the start of every cycle (see
    ``CycleRunner.run``), so a cycle that runs long never drifts the next
    one -- the one after a late cycle is still the real next ``:00``/``:30``
    on the wall clock, possibly with less lead time, never more.
    """
    if now.tzinfo is None:
        raise KeepaliveError("keepalive clock returned a naive datetime")
    based = now.replace(second=0, microsecond=0)
    if now.minute < 30:
        return based.replace(minute=30)
    return (based.replace(minute=0)) + dt.timedelta(hours=1)


class Provider(Protocol):
    key: str
    display_name: str

    def detect_activity(self) -> dt.datetime | None: ...
    def ping(self) -> PingResult: ...


def initial_state(provider: Provider) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "agent": provider.key,
        # Operationally sufficient for diagnostics: when the last *ping*
        # (not slot -- a SKIP never touches this) finished and whether it
        # succeeded. Nothing about tokens, cache, cost, or classification is
        # ever stored here.
        "last_ping_finished_at": None,
        "last_status": None,
        "last_error": None,
        "updated_at": None,
    }


class KeepaliveAgent:
    """One provider's slot logic: decide, then ping or don't.

    Deliberately still minimal: the *decision* (PING vs SKIP) is the one
    documented, fixed-window interpretation this module performs; once
    ``PING`` is chosen, the ping's own outcome is read only as
    ``PingResult.ok``/``error`` -- nothing about a ping's output is ever
    interpreted here, exactly as before.
    """

    def __init__(self, provider: Provider, state_path: Path, *,
                 clock: Callable[[], dt.datetime] = utc_now,
                 activity_window: int = ACTIVITY_WINDOW_SECONDS):
        self.provider = provider
        self.state_path = state_path
        self.clock = clock
        self.activity_window = activity_window

    def load(self) -> tuple[dict[str, Any], bool]:
        state = _load_state(self.state_path)
        if state is None:
            return initial_state(self.provider), False
        if state.get("agent") != self.provider.key:
            raise KeepaliveError("persisted keepalive identity does not match provider")
        return state, True

    def decide(self, now: dt.datetime, last_activity: dt.datetime | None) -> str:
        """``SKIP`` iff ``last_activity`` is within ``activity_window`` of
        ``now``; ``PING`` otherwise (including when activity is unknown --
        unknown is never treated as "recently active").
        """
        if last_activity is None:
            return ACTION_PING
        age = (now - last_activity).total_seconds()
        return ACTION_SKIP if 0 <= age <= self.activity_window else ACTION_PING

    def _do_ping(self) -> dict[str, Any]:
        """Run the ping and persist state, timestamped by when it actually
        *finished* -- deliberately read fresh from the clock only after
        ``provider.ping()`` returns (a ping can legitimately take anywhere
        up to its own timeout), never before it, so ``last_ping_finished_at``
        and a PING event's own ``timestamp`` never understate how long the
        ping actually took.
        """
        state, _resumed = self.load()
        result = self.provider.ping()
        finished = self.clock()
        status = "ok" if result.ok else "fail"
        state["last_ping_finished_at"] = iso(finished)
        state["last_status"] = status
        state["last_error"] = None if result.ok else result.error
        state["updated_at"] = iso(finished)
        save_state(self.state_path, state)
        return {"status": status, "error": state["last_error"], "finished_at": finished}

    def run_slot(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """One activity-aware slot evaluation for this agent, right now.

        Reads this agent's own activity once, decides, and -- only for
        ``PING`` -- runs the ping and persists scheduler state exactly as
        before. A ``SKIP`` never calls ``provider.ping()`` at all and never
        touches the state file (nothing ping-related happened).
        """
        when = now if now is not None else self.clock()
        last_activity = self.provider.detect_activity()
        action = self.decide(when, last_activity)
        record: dict[str, Any] = {
            "agent": self.provider.key,
            "action": action,
            "last_activity": iso(last_activity) if last_activity is not None else None,
            "ping_status": None,
            "ping_error": None,
        }
        if action == ACTION_PING:
            outcome = self._do_ping()
            record["timestamp"] = iso(outcome["finished_at"])
            record["ping_status"] = outcome["status"]
            record["ping_error"] = outcome["error"]
        else:
            record["timestamp"] = iso(when)
        return record

    def ping_once(self, on_status: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Exactly one immediate, forced ping -- bypasses the activity-aware
        decision entirely (never ``SKIP``) and never waits.

        For live/diagnostic use only (CLI ``--once``): a deliberate escape
        hatch confirming the ping mechanism itself works right now,
        independent of whether a scheduled slot would have skipped it.
        """
        outcome = self._do_ping()
        record = {
            "agent": self.provider.key, "action": ACTION_PING, "timestamp": iso(outcome["finished_at"]),
            "last_activity": None, "ping_status": outcome["status"], "ping_error": outcome["error"],
        }
        if on_status:
            on_status(record)
        return record


class CycleRunner:
    """Runs fixed :00/:30 cycles over a fixed, ordered list of agents.

    Each cycle: compute the next real boundary, wait for it, then visit
    every agent in ``agents`` order at ``boundary + index * stagger``. One
    agent's ping failure or unexpected exception is caught and recorded as
    its own event; it never prevents a later agent's slot in the same cycle,
    and it never affects the next cycle's boundary (recomputed fresh from
    the wall clock, not from this cycle's start/end time).
    """

    def __init__(self, agents: list[tuple[str, "KeepaliveAgent"]], *,
                 clock: Callable[[], dt.datetime] = utc_now,
                 sleeper: Callable[[float], None] = time.sleep,
                 stagger: int = AGENT_STAGGER_SECONDS,
                 poll_interval: float = 5.0):
        self.agents = agents
        self.clock = clock
        self.sleeper = sleeper
        self.stagger = stagger
        self.poll_interval = max(0.05, poll_interval)

    def _sleep_until(self, deadline: dt.datetime) -> None:
        while True:
            remaining = (deadline - self.clock()).total_seconds()
            if remaining <= 0:
                return
            self.sleeper(min(self.poll_interval, remaining))

    def run_cycle(self, boundary: dt.datetime,
                 on_event: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
        records = []
        for index, (key, agent) in enumerate(self.agents):
            slot_at = boundary + dt.timedelta(seconds=index * self.stagger)
            self._sleep_until(slot_at)
            try:
                record = agent.run_slot(self.clock())
            except Exception as exc:
                now = self.clock()
                record = {
                    "agent": key, "action": "error", "timestamp": iso(now),
                    "last_activity": None, "ping_status": "fail",
                    "ping_error": f"slot error: {exc!r}",
                }
            records.append(record)
            if on_event:
                on_event(record)
        return records

    def run(self, *, max_cycles: int | None = None,
            on_event: Callable[[dict[str, Any]], None] | None = None,
            on_wait: Callable[[dict[str, Any]], None] | None = None) -> int:
        count = 0
        while max_cycles is None or count < max_cycles:
            boundary = next_boundary(self.clock())
            if on_wait:
                on_wait({"scheduled_at": iso(boundary)})
            self._sleep_until(boundary)
            self.run_cycle(boundary, on_event=on_event)
            count += 1
        return 0
