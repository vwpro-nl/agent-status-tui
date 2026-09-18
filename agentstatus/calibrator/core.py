"""Scheduling and controller logic with no provider-specific measurements."""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any, Callable

from .model import STATUS_ACTIVITY, STATUS_UNRELIABLE, Adapter
from .persistence import CONTROLLER_STATE_SCHEMA, STATE_SCHEMA, append_history, load_state, save_state

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
        # Reserved for a future controller/strategy layer (KEEP/LET_EXPIRE,
        # economics, boundary search). Intentionally opaque and untouched by
        # this phase -- versioned so a later phase can migrate it safely.
        "controller": {"schema": CONTROLLER_STATE_SCHEMA},
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
        return state, True

    def _probe_floor(self, sampling: dict[str, Any]) -> dt.datetime:
        floor_text = sampling.get("last_probe_finished_at")
        # A missing floor (no prior probe recorded yet) anchors on "now",
        # never on an activity source's own timestamp and never on an
        # invented epoch -- both would risk collapsing the deadline into the
        # past and firing an unwarranted immediate probe.
        return _parse_iso(floor_text) if isinstance(floor_text, str) else self.clock()

    def _wait_for_interval(self, sampling: dict[str, Any], interval: int) -> None:
        floor = self._probe_floor(sampling)
        known_at = None
        result = self.adapter.detect_activity()
        if result.status == STATUS_ACTIVITY:
            known_at = result.at
        elif result.status == STATUS_UNRELIABLE:
            self._record("activity-unreliable", self.clock(), failed_checks=list(result.failed_checks))
        anchor = max(known_at, floor) if known_at is not None else floor
        deadline = anchor + dt.timedelta(seconds=interval)
        while self.clock() < deadline:
            self.sleeper(min(self.check_interval, (deadline - self.clock()).total_seconds()))
            newer = self.adapter.detect_activity()
            if newer.status == STATUS_ACTIVITY and (known_at is None or newer.at > known_at):
                # Newer, reliably-detected activity reschedules the deadline
                # forward. "none" and "unreliable" never do this: a boundary
                # search that used bad/missing activity data to shorten or
                # skip the wait would risk an unwarranted probe.
                known_at = newer.at
                deadline = newer.at + dt.timedelta(seconds=interval)
                self._record("activity-detected", self.clock(), source=newer.source,
                             scheduled_at=iso(deadline))
            elif newer.status == STATUS_UNRELIABLE:
                # Fail-closed retry: an unreliable check is never silently
                # read as "no activity". It changes nothing about the
                # deadline and is simply retried on the next tick.
                self._record("activity-unreliable", self.clock(), failed_checks=list(newer.failed_checks))

    def run(self, *, max_measurements: int | None = None,
            emit: Callable[[dict[str, Any]], None] | None = None) -> int:
        state, resumed = self.load()
        now = self.clock()
        self._record("run-started", now, resumed=resumed)
        completed = 0
        while max_measurements is None or completed < max_measurements:
            sampling = state["sampling"]
            startup = not sampling["startup_complete"]
            interval = None if startup else int(sampling["next_interval_seconds"])
            if not startup:
                self._wait_for_interval(sampling, interval)
            observation = self.adapter.probe()
            finished = self.clock()
            assessment = self.adapter.assess(observation, state.get("baseline", {}), startup=startup)
            success = observation.valid and assessment.classification != "fail"
            previous = sampling.get("last_measured_interval_seconds")
            movement = None if interval is None or previous is None else interval - int(previous)
            prospective_progress = int(sampling["progress"]) + (0 if startup else 1)
            prospective_next = (VIRGIN_INTERVALS[0] if startup else
                                _next_interval(prospective_progress, int(interval)))
            record = self._record(
                "measurement", finished, measurement_id=observation.measurement_id,
                interval_seconds=interval, valid=observation.valid,
                classification=assessment.classification, values=dict(observation.values),
                display=dict(observation.display), error=observation.error,
                movement_seconds=movement, update_baseline=assessment.update_baseline,
                next_scheduled_at=iso(finished + dt.timedelta(seconds=prospective_next)) if success else None,
            )
            if emit:
                emit(record)
            if not success:
                return 1
            if assessment.update_baseline:
                state["baseline"] = dict(assessment.baseline)
            sampling["last_probe_finished_at"] = iso(finished)
            if startup:
                sampling["startup_complete"] = True
            else:
                sampling["last_measured_interval_seconds"] = interval
                sampling["progress"] = int(sampling["progress"]) + 1
                sampling["next_interval_seconds"] = _next_interval(int(sampling["progress"]), interval)
                completed += 1
            state["updated_at"] = iso(finished)
            save_state(self.state_path, state)
            # The next quiet interval starts at probe completion, even if the
            # provider's local activity evidence has not appeared yet.
            if max_measurements is not None and completed >= max_measurements:
                break
        return 0
