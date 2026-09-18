"""Scheduling and controller logic with no provider-specific measurements."""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any, Callable

from .controller import (
    MODE_DONE, apply as apply_controller, controller_schema_is_current, empty_controller,
    ensure_controller, migrate_legacy_controller,
)
from .model import STATUS_ACTIVITY, STATUS_UNRELIABLE, Adapter
from .persistence import STATE_SCHEMA, append_history, load_history, load_state, save_state

VIRGIN_INTERVALS = (60, 60, 120, 180, 240, 300)
LINEAR_STEP = 300


class CalibratorError(Exception):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise CalibratorError("calibrator clock returned a naive datetime")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def initial_state(adapter: Adapter) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "agent": adapter.agent,
        "provider": adapter.provider,
        "measurement_schema": adapter.measurement_schema,
        # Sampling/explorer state: progress through the virgin ladder, the
        # next planned interval, and the probe-floor used to anchor activity
        # checks. A future controller reads this; it does not write it.
        "sampling": {
            "startup_complete": False,
            "progress": 0,
            "next_interval_seconds": VIRGIN_INTERVALS[0],
            "last_measured_interval_seconds": None,
            "last_probe_finished_at": None,
        },
        # Adapter-owned provider assessment. Only ever replaced when the
        # adapter's Assessment sets update_baseline=True.
        "baseline": {},
        "controller": empty_controller(),
        "updated_at": None,
    }


def _next_interval(progress: int, measured: int) -> int:
    return VIRGIN_INTERVALS[progress] if progress < len(VIRGIN_INTERVALS) else measured + LINEAR_STEP


class Calibrator:
    def __init__(self, adapter: Adapter, state_path: Path, history_path: Path,
                 *, clock: Callable[[], dt.datetime] = utc_now,
                 sleeper: Callable[[float], None] = time.sleep,
                 check_interval: float = 60.0):
        self.adapter = adapter
        self.state_path = state_path
        self.history_path = history_path
        self.clock = clock
        self.sleeper = sleeper
        self.check_interval = max(0.1, check_interval)

    def _record(self, event: str, when: dt.datetime, **fields: Any) -> dict[str, Any]:
        record = {
            "event": event, "timestamp": iso(when), "agent": self.adapter.agent,
            "provider": self.adapter.provider,
            "measurement_schema": self.adapter.measurement_schema, **fields,
        }
        append_history(self.history_path, record)
        return record

    def load(self) -> tuple[dict[str, Any], bool]:
        state = load_state(self.state_path)
        if state is None:
            return initial_state(self.adapter), False
        identity = (state.get("agent"), state.get("provider"), state.get("measurement_schema"))
        expected = (self.adapter.agent, self.adapter.provider, self.adapter.measurement_schema)
        if identity != expected:
            raise CalibratorError("persisted calibrator identity does not match adapter")
        raw_controller = state.get("controller")
        if controller_schema_is_current(raw_controller):
            state["controller"] = ensure_controller(raw_controller)
        else:
            state = migrate_legacy_controller(state, load_history(self.history_path))
            save_state(self.state_path, state)
        return state, True

    def _probe_floor(self, sampling: dict[str, Any]) -> dt.datetime:
        floor_text = sampling.get("last_probe_finished_at")
        # A missing floor (no prior probe recorded yet) anchors on "now",
        # never on an activity source's own timestamp and never on an
        # invented epoch -- both would risk collapsing the deadline into the
        # past and firing an unwarranted immediate probe.
        return _parse_iso(floor_text) if isinstance(floor_text, str) else self.clock()

    def _emit_wait(self, on_wait: Callable[[dict[str, Any]], None] | None,
                   interval: int, deadline: dt.datetime, reason: str) -> None:
        if on_wait is None:
            return
        on_wait({
            "event": "wait-status",
            "timestamp": iso(self.clock()),
            "agent": self.adapter.agent,
            "provider": self.adapter.provider,
            "interval_seconds": interval,
            "scheduled_at": iso(deadline),
            "reason": reason,
        })

    def _wait_for_interval(self, sampling: dict[str, Any], interval: int,
                           on_wait: Callable[[dict[str, Any]], None] | None = None,
                           floor_at_least: dt.datetime | None = None) -> dict[str, Any]:
        floor = self._probe_floor(sampling)
        if floor_at_least is not None:
            # First wait after resume: silence from before this run must not
            # count toward a new boundary sample.
            floor = max(floor, floor_at_least)
        known_at = None
        result = self.adapter.detect_activity()
        if result.status == STATUS_ACTIVITY:
            known_at = result.at
        elif result.status == STATUS_UNRELIABLE:
            self._record("activity-unreliable", self.clock(), failed_checks=list(result.failed_checks))
        anchor = max(known_at, floor) if known_at is not None else floor
        deadline = anchor + dt.timedelta(seconds=interval)
        self._emit_wait(on_wait, interval, deadline, "initial")
        while self.clock() < deadline:
            self.sleeper(min(self.check_interval, (deadline - self.clock()).total_seconds()))
            newer = self.adapter.detect_activity()
            if newer.status == STATUS_ACTIVITY and (known_at is None or newer.at > known_at):
                if floor_at_least is not None and newer.at <= floor_at_least:
                    known_at = newer.at
                else:
                    # Newer, reliably-detected activity reschedules the deadline
                    # forward. "none" and "unreliable" never do this: a boundary
                    # search that used bad/missing activity data to shorten or
                    # skip the wait would risk an unwarranted probe.
                    known_at = newer.at
                    deadline = newer.at + dt.timedelta(seconds=interval)
                    self._record("activity-detected", self.clock(), source=newer.source,
                                 scheduled_at=iso(deadline))
                    self._emit_wait(on_wait, interval, deadline, "rescheduled")
            elif newer.status == STATUS_UNRELIABLE:
                # Fail-closed retry: an unreliable check is never silently
                # read as "no activity". It changes nothing about the
                # deadline and is simply retried on the next tick.
                self._record("activity-unreliable", self.clock(), failed_checks=list(newer.failed_checks))
        started = self.clock()
        silence = max(0, int(round((started - anchor).total_seconds())))
        return {
            "wait_anchor_at": iso(anchor),
            "scheduled_at": iso(deadline),
            "actual_silence_seconds": silence,
        }

    def run(self, *, max_measurements: int | None = None,
            emit: Callable[[dict[str, Any]], None] | None = None,
            on_wait: Callable[[dict[str, Any]], None] | None = None) -> int:
        state, resumed = self.load()
        now = self.clock()
        self._record("run-started", now, resumed=resumed)
        if (state.get("controller") or {}).get("mode") == MODE_DONE:
            # Already terminal from a prior run: nothing left to probe or wait
            # for. Existing result/recommended state stays untouched.
            return 0
        completed = 0
        resume_floor = now if resumed else None
        while max_measurements is None or completed < max_measurements:
            sampling = state["sampling"]
            startup = not sampling["startup_complete"]
            interval = None if startup else int(sampling["next_interval_seconds"])
            wait_info = None
            if not startup:
                wait_info = self._wait_for_interval(
                    sampling, interval, on_wait=on_wait, floor_at_least=resume_floor)
            observation = self.adapter.probe()
            finished = self.clock()
            assessment = self.adapter.assess(observation, state.get("baseline", {}), startup=startup)
            success = observation.valid and assessment.classification != "fail"
            previous = sampling.get("last_measured_interval_seconds")
            movement = None if interval is None or previous is None else interval - int(previous)
            controller, sampling_updates = apply_controller(
                sampling, state.get("controller"),
                interval=interval, classification=assessment.classification,
                valid=observation.valid and assessment.classification != "fail",
            )
            next_interval = sampling_updates["next_interval_seconds"]
            if not success and startup:
                # Startup is not an interval measurement; a failed startup
                # must not advertise a 1m wait as if the explorer had begun.
                next_interval = None
            next_movement = (
                next_interval - int(interval)
                if next_interval is not None and interval is not None else None
            )
            record = self._record(
                "measurement", finished, measurement_id=observation.measurement_id,
                interval_seconds=interval, valid=observation.valid,
                classification=assessment.classification, values=dict(observation.values),
                display=dict(observation.display), error=observation.error,
                movement_seconds=movement, update_baseline=assessment.update_baseline,
                next_interval_seconds=next_interval,
                next_movement_seconds=next_movement,
                next_scheduled_at=(iso(finished + dt.timedelta(seconds=next_interval))
                                   if next_interval is not None else None),
                wait_anchor_at=None if wait_info is None else wait_info["wait_anchor_at"],
                scheduled_wait_at=None if wait_info is None else wait_info["scheduled_at"],
                actual_silence_seconds=None if wait_info is None else wait_info["actual_silence_seconds"],
            )
            if emit:
                emit(record)
            if not success:
                return 1
            if assessment.update_baseline:
                state["baseline"] = dict(assessment.baseline)
            state["controller"] = controller
            sampling["last_probe_finished_at"] = iso(finished)
            sampling["next_interval_seconds"] = sampling_updates["next_interval_seconds"]
            sampling["progress"] = sampling_updates["progress"]
            sampling["last_measured_interval_seconds"] = sampling_updates["last_measured_interval_seconds"]
            if startup:
                sampling["startup_complete"] = True
            else:
                completed += 1
                resume_floor = None
            state["updated_at"] = iso(finished)
            save_state(self.state_path, state)
            if controller.get("mode") == MODE_DONE:
                # This measurement just reached the terminal state: it has
                # already been emitted and persisted atomically above. There
                # is nothing left to wait or probe for.
                return 0
            # The next quiet interval starts at probe completion, even if the
            # provider's local activity evidence has not appeared yet.
            if max_measurements is not None and completed >= max_measurements:
                break
        return 0
