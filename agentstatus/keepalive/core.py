"""Fixed-interval, provider-agnostic keepalive scheduler.

No sampling ladder, no controller, no boundary search, no good/bad
classification, no token/cache/quota/billing interpretation. One
``KeepaliveAgent`` per provider, each with its own persisted state and a
fixed interval since either its own last ping or its own last *detected
real activity* -- whichever is later. A ping's outcome is read only as
``PingResult.ok``/``error`` (see :mod:`agentstatus.keepalive.providers`) --
nothing about a ping's output is ever interpreted here.

Multiple agents are independent by construction: each ``KeepaliveAgent``
only ever reads its own provider's ``detect_activity()`` and its own state
file, so one agent's activity can never shift another's deadline.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .persistence import STATE_SCHEMA, load_state as _load_state, save_state
from .providers import PingResult

# Fixed by contract, never adaptive, never derived from a search -- the same
# value for every agent, every cycle.
#
# 3001 seconds (50m01s) is a deliberate system parameter, not a rounding of
# 3000: 6 * 3001 = 18006s = 5h00m06s. Under uninterrupted operation, every
# sixth keepalive lands only ~6 seconds past a 5-hour period -- 6 keepalive
# calls per 5 hours instead of 10 at a 3000s/50m interval. Do not round this
# to 3000.
INTERVAL_SECONDS = 3001


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


class Provider(Protocol):
    key: str
    display_name: str

    def detect_activity(self) -> dt.datetime | None: ...
    def ping(self) -> PingResult: ...


def initial_state(provider: Provider) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "agent": provider.key,
        # Operationally sufficient for restart/resume: when the last ping
        # finished and whether it succeeded. Nothing about tokens, cache,
        # cost, or classification is ever stored here.
        "last_ping_finished_at": None,
        "last_status": None,
        "last_error": None,
        "updated_at": None,
    }


class KeepaliveAgent:
    def __init__(self, provider: Provider, state_path: Path, *,
                 clock: Callable[[], dt.datetime] = utc_now,
                 sleeper: Callable[[float], None] = time.sleep,
                 check_interval: float = 30.0,
                 interval: int = INTERVAL_SECONDS):
        self.provider = provider
        self.state_path = state_path
        self.clock = clock
        self.sleeper = sleeper
        self.check_interval = max(0.1, check_interval)
        self.interval = interval

    def load(self) -> tuple[dict[str, Any], bool]:
        state = _load_state(self.state_path)
        if state is None:
            return initial_state(self.provider), False
        if state.get("agent") != self.provider.key:
            raise KeepaliveError("persisted keepalive identity does not match provider")
        return state, True

    def _floor(self, state: dict[str, Any]) -> dt.datetime:
        text = state.get("last_ping_finished_at")
        # A missing floor (never pinged, or first run) anchors on "now" --
        # never an invented epoch.
        return _parse_iso(text) if isinstance(text, str) else self.clock()

    def _emit_wait(self, on_wait: Callable[[dict[str, Any]], None] | None,
                   deadline: dt.datetime) -> None:
        if on_wait is None:
            return
        on_wait({
            "agent": self.provider.key,
            "timestamp": iso(self.clock()), "scheduled_at": iso(deadline),
        })

    def _wait(self, state: dict[str, Any],
              on_wait: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Wait until this agent's own deadline. Unrelated to any other agent.

        deadline = anchor + interval, where anchor starts at max(own
        last-ping floor, own last detected real activity) and can only ever
        increase during the wait: a newly discovered activity timestamp
        reschedules the deadline forward only when it is newer than the
        current anchor, never merely newer than whatever was previously
        known -- so a timestamp that surfaces late but predates the anchor
        (e.g. floor) can never pull an already-planned deadline earlier.
        Restart/resume honors a stale persisted floor as-is: if it is
        already in the past, this returns immediately rather than granting
        a fresh interval.
        """
        floor = self._floor(state)
        initial_activity = self.provider.detect_activity()
        anchor = max(initial_activity, floor) if initial_activity is not None else floor
        deadline = anchor + dt.timedelta(seconds=self.interval)
        self._emit_wait(on_wait, deadline)
        while self.clock() < deadline:
            self.sleeper(min(self.check_interval, (deadline - self.clock()).total_seconds()))
            newer = self.provider.detect_activity()
            if newer is not None and newer > anchor:
                anchor = newer
                deadline = anchor + dt.timedelta(seconds=self.interval)
                self._emit_wait(on_wait, deadline)
            # No activity found, or found activity no newer than the
            # current anchor: silently keep ticking toward the existing
            # deadline. anchor never decreases, so deadline never moves
            # earlier.
        return {"anchor_at": iso(anchor), "scheduled_at": iso(deadline)}

    def ping(self, state: dict[str, Any]) -> dict[str, Any]:
        """One minimal provider probe.

        Only ``PingResult.ok``/``error`` are read. No classification, no
        token/cache/quota interpretation happens here or anywhere else in
        this module.
        """
        result = self.provider.ping()
        finished = self.clock()
        status = "ok" if result.ok else "fail"
        state["last_ping_finished_at"] = iso(finished)
        state["last_status"] = status
        state["last_error"] = None if result.ok else result.error
        state["updated_at"] = iso(finished)
        save_state(self.state_path, state)
        return {
            "agent": self.provider.key, "finished_at": iso(finished),
            "status": status, "error": state["last_error"],
        }

    def ping_once(self, on_status: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Exactly one immediate probe -- no wait, regardless of any
        persisted deadline or activity.

        For live/diagnostic use only (CLI ``--once``): a deliberate escape
        hatch from the normal scheduled cadence, not "one normal cycle".
        The normal 3001s-since-last-ping/activity cadence only ever happens
        through ``run()``/``_wait()``, which this never calls.
        """
        state, _resumed = self.load()
        record = self.ping(state)
        if on_status:
            on_status(record)
        return record

    def run(self, *, max_pings: int | None = None,
            on_status: Callable[[dict[str, Any]], None] | None = None,
            on_wait: Callable[[dict[str, Any]], None] | None = None) -> int:
        """Wait-then-ping, forever (or ``max_pings`` times for tests).

        A failed ping is not retried quickly: ``ping()`` unconditionally
        advances ``last_ping_finished_at`` to *now*, whether it succeeded or
        not, so the very next ``_wait()`` starts a fresh full interval either
        way -- unless real activity detected during that wait pushes the
        deadline later still.
        """
        state, _resumed = self.load()
        count = 0
        while max_pings is None or count < max_pings:
            self._wait(state, on_wait=on_wait)
            record = self.ping(state)
            if on_status:
                on_status(record)
            count += 1
        return 0
