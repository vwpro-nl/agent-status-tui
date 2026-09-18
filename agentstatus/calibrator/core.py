"""Scheduling and controller logic with no provider-specific measurements."""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any, Callable

from .model import Adapter
from .persistence import STATE_SCHEMA, append_history, load_state, save_state

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


def initial_state(adapter: Adapter) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "agent": adapter.agent,
        "provider": adapter.provider,
        "measurement_schema": adapter.measurement_schema,
        "startup_complete": False,
        "progress": 0,
        "next_interval_seconds": VIRGIN_INTERVALS[0],
        "last_measured_interval_seconds": None,
        "baseline": {},
        "last_probe_finished_at": None,
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

    def run(self, *, max_measurements: int | None = None,
            emit: Callable[[dict[str, Any]], None] | None = None) -> int:
        state, resumed = self.load()
        now = self.clock()
        self._record("run-started", now, resumed=resumed)
        completed = 0
        while max_measurements is None or completed < max_measurements:
            startup = not state["startup_complete"]
            interval = None if startup else int(state["next_interval_seconds"])
            if not startup:
                activity = self.adapter.detect_activity()
                floor_text = state.get("last_probe_finished_at")
                floor = (dt.datetime.fromisoformat(floor_text.replace("Z", "+00:00"))
                         if isinstance(floor_text, str) else activity.at)
                anchor = max(activity.at, floor)
                deadline = anchor + dt.timedelta(seconds=interval)
                while self.clock() < deadline:
                    self.sleeper(min(self.check_interval, (deadline - self.clock()).total_seconds()))
                    newer = self.adapter.detect_activity()
                    if newer.at > activity.at:
                        activity = newer
                        deadline = activity.at + dt.timedelta(seconds=interval)
                        self._record("activity-detected", self.clock(), source=activity.source,
                                     scheduled_at=iso(deadline))
            observation = self.adapter.probe()
            finished = self.clock()
            assessment = self.adapter.assess(observation, state.get("baseline", {}))
            success = observation.valid and assessment.classification != "fail"
            previous = state.get("last_measured_interval_seconds")
            movement = None if interval is None or previous is None else interval - int(previous)
            prospective_progress = int(state["progress"]) + (0 if startup else 1)
            prospective_next = (VIRGIN_INTERVALS[0] if startup else
                                _next_interval(prospective_progress, int(interval)))
            record = self._record(
                "measurement", finished, measurement_id=observation.measurement_id,
                interval_seconds=interval, valid=observation.valid,
                classification=assessment.classification, values=dict(observation.values),
                display=dict(observation.display), error=observation.error,
                movement_seconds=movement,
                next_scheduled_at=iso(finished + dt.timedelta(seconds=prospective_next)) if success else None,
            )
            if emit:
                emit(record)
            if not success:
                return 1
            state["baseline"] = dict(assessment.baseline)
            state["last_probe_finished_at"] = iso(finished)
            if startup:
                state["startup_complete"] = True
            else:
                state["last_measured_interval_seconds"] = interval
                state["progress"] = int(state["progress"]) + 1
                state["next_interval_seconds"] = _next_interval(int(state["progress"]), interval)
                completed += 1
            state["updated_at"] = iso(finished)
            save_state(self.state_path, state)
            # The next quiet interval starts at probe completion, even if the
            # provider's local activity evidence has not appeared yet.
            if max_measurements is not None and completed >= max_measurements:
                break
        return 0
