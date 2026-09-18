"""Provider-neutral calibrator types and adapter boundary."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Mapping, Protocol


@dataclasses.dataclass(frozen=True)
class Activity:
    at: dt.datetime
    source: str


@dataclasses.dataclass(frozen=True)
class Observation:
    valid: bool
    measurement_id: str
    values: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    display: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class Assessment:
    classification: str
    baseline: Mapping[str, Any] = dataclasses.field(default_factory=dict)


class Adapter(Protocol):
    provider: str
    agent: str
    display_name: str
    measurement_schema: str
    display_columns: tuple[tuple[str, str], ...]

    def detect_activity(self) -> Activity: ...
    def probe(self) -> Observation: ...
    def assess(self, observation: Observation, baseline: Mapping[str, Any]) -> Assessment: ...
