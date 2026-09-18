"""Provider-neutral calibrator types and adapter boundary.

Five concerns are kept explicitly separate here and in the sibling modules:

1. measurement acquisition   -- ``Adapter.probe`` / ``Observation``
2. measurement history       -- ``persistence`` (append-only, per agent)
3. sampling ladder / explorer -- ``core.Calibrator`` (progress, next interval)
4. provider assessment       -- ``Adapter.assess`` / ``Assessment`` (baseline)
5. controller/strategy       -- not implemented yet; reserved state only

The sampling ladder collects history. A future controller will interpret
that history but does not, and must not, implicitly steer the explorer.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Mapping, Protocol

# ActivityResult.status values. A provider-neutral replacement for the old
# timestamp/exception activity signal: "did we find newer user activity" now
# has three outcomes instead of two, so a broken activity source can never be
# silently read as "the user is idle".
STATUS_ACTIVITY = "activity"
STATUS_NONE = "none"
STATUS_UNRELIABLE = "unreliable"
_ACTIVITY_STATUSES = frozenset({STATUS_ACTIVITY, STATUS_NONE, STATUS_UNRELIABLE})


@dataclasses.dataclass(frozen=True)
class ActivityResult:
    """The outcome of one activity check, with the evidence behind it.

    - ``activity``: at least one reliably-checked source found newer
      activity; ``at`` carries that instant.
    - ``none``: every relevant source was reliably checked and none found
      activity. There is no timestamp to report -- the scheduler must anchor
      on its own probe-floor, never on an invented epoch or "user idle"
      guess.
    - ``unreliable``: no source could be reliably checked (e.g. every source
      errored). This must never be treated as ``none``: it means "we don't
      know", not "no activity".
    """

    status: str
    at: dt.datetime | None = None
    reliable_checks: int = 0
    failed_checks: tuple[str, ...] = ()
    source: str | None = None
    details: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _ACTIVITY_STATUSES:
            raise ValueError(f"invalid ActivityResult status: {self.status!r}")
        if self.status == STATUS_ACTIVITY:
            if self.at is None:
                raise ValueError("ActivityResult status 'activity' requires 'at'")
            if self.at.tzinfo is None:
                raise ValueError("ActivityResult 'at' must be timezone-aware")
        elif self.at is not None:
            raise ValueError(f"ActivityResult status {self.status!r} must not carry 'at'")


@dataclasses.dataclass(frozen=True)
class Observation:
    valid: bool
    measurement_id: str
    values: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    display: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class Assessment:
    """A provider's read of one measurement, plus its baseline verdict.

    ``update_baseline`` is the explicit contract for baseline ownership: the
    generic core never assumes a valid measurement changes the baseline. It
    only writes ``baseline`` into persisted state when the adapter sets this
    flag. Everything about *when* that is true (good vs. bad vs. init vs.
    startup) is entirely the adapter's call.
    """

    classification: str
    baseline: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    update_baseline: bool = False


class Adapter(Protocol):
    provider: str
    agent: str
    display_name: str
    measurement_schema: str
    def detect_activity(self) -> ActivityResult: ...
    def probe(self) -> Observation: ...
    def assess(self, observation: Observation, baseline: Mapping[str, Any],
               *, startup: bool = False) -> Assessment: ...
