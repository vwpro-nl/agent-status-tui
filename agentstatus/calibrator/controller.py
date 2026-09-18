"""Provider-neutral boundary controller.

Consumes only valid good/bad interval samples. Does not read token/cost/quota
fields. sampling.next_interval_seconds remains the single planned interval.
"""

from __future__ import annotations

from typing import Any

from .persistence import CONTROLLER_STATE_SCHEMA

MODE_EXPLORE = "explore"
MODE_CONFIRM = "confirm"
MODE_BRACKET = "bracket"
MODE_DONE = "done"
MAX_SAMPLES = 3
MIN_BRACKET_SPAN = 60

# Adapter.assess() returns whatever classification string fits a given
# provider's own probe semantics (see model.Assessment). The controller only
# understands two evidence labels -- "good" and "bad" -- plus "not usable as
# evidence" (fail, init, ...). Providers without boundary-crossing detection
# (Codex, Grok: see their assess(), which only ever returns "ok" or "fail")
# report every valid, non-failing measurement as "ok" -- a successful
# interval that never signalled a boundary. That is evidence-equivalent to
# "good": these adapters simply have no way to emit "bad". This is a
# classification-string contract, not a per-provider carve-out -- any
# adapter using this vocabulary gets the same normalization.
_GOOD_LABELS = frozenset({"good", "ok"})
_BAD_LABELS = frozenset({"bad"})


def _evidence_label(classification: str) -> str | None:
    if classification in _GOOD_LABELS:
        return "good"
    if classification in _BAD_LABELS:
        return "bad"
    return None


def _explorer_next(progress: int, measured: int) -> int:
    from .core import _next_interval
    return _next_interval(progress, measured)


def empty_controller() -> dict[str, Any]:
    return {
        "schema": CONTROLLER_STATE_SCHEMA,
        "mode": MODE_EXPLORE,
        "known_good_seconds": None,
        "confirmed_bad_seconds": None,
        "candidate_seconds": None,
        "evidence": {},
        "result": None,
    }


def ensure_controller(raw: Any) -> dict[str, Any]:
    """Normalize an already-current v2 controller. Does not read history."""
    if not isinstance(raw, dict) or raw.get("schema") != CONTROLLER_STATE_SCHEMA:
        return empty_controller()
    merged = empty_controller()
    for key in merged:
        if key in raw:
            merged[key] = raw[key]
    if not isinstance(merged["evidence"], dict):
        merged["evidence"] = {}
    return merged


def controller_schema_is_current(raw: Any) -> bool:
    return isinstance(raw, dict) and raw.get("schema") == CONTROLLER_STATE_SCHEMA


def _usable_measurement(record: Any) -> bool:
    if not isinstance(record, dict) or record.get("event") != "measurement":
        return False
    if record.get("valid") is not True:
        return False
    if not isinstance(record.get("interval_seconds"), int):
        return False
    return record.get("classification") in ("good", "bad")


def migrate_legacy_controller(state: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Bootstrap v2 controller from a finished legacy explorer run.

    Does not replay the whole history through apply(). Only the current
    frontier (last_measured_interval_seconds) can enter confirm; older bads
    behind that frontier stay out of mode/candidate.
    """
    sampling = state.setdefault("sampling", {})
    controller = empty_controller()
    frontier = sampling.get("last_measured_interval_seconds")
    usable = [record for record in records if _usable_measurement(record)]
    usable.sort(key=lambda item: item.get("timestamp") or "")
    if not isinstance(frontier, int):
        state["controller"] = controller
        return state
    frontier_hits = [item for item in usable if item["interval_seconds"] == frontier]
    if not frontier_hits:
        state["controller"] = controller
        return state
    label = frontier_hits[-1]["classification"]
    if label == "bad":
        controller["mode"] = MODE_CONFIRM
        controller["candidate_seconds"] = frontier
        controller["evidence"][_key(frontier)] = ["bad"]
        below = [item["interval_seconds"] for item in usable
                 if item["classification"] == "good" and item["interval_seconds"] < frontier]
        if below:
            low = max(below)
            controller["evidence"][_key(low)] = ["good"]
        original_next = sampling.get("next_interval_seconds")
        progress = int(sampling.get("progress") or 0)
        # After a legacy measure of `frontier`, progress is already incremented
        # and next = _next_interval(progress, frontier). Only then roll back.
        expected_next = _explorer_next(progress, frontier) if progress > 0 else None
        sampling["next_interval_seconds"] = frontier
        if original_next == expected_next and progress > 0:
            sampling["progress"] = progress - 1
    else:
        controller["evidence"][_key(frontier)] = ["good"]
    state["controller"] = controller
    return state


def _key(interval: int) -> str:
    return str(int(interval))


def _labels(controller: dict[str, Any], interval: int) -> list[str]:
    values = controller["evidence"].get(_key(interval), [])
    return [item for item in values if item in ("good", "bad")]


def decision(labels: list[str]) -> str | None:
    """2-of-max-3. A 2-1 split is decided. None means more samples needed."""
    goods = labels.count("good")
    bads = labels.count("bad")
    if goods >= 2:
        return "good"
    if bads >= 2:
        return "bad"
    return None


def _add_sample(controller: dict[str, Any], interval: int, label: str) -> None:
    key = _key(interval)
    samples = list(controller["evidence"].get(key, []))
    if decision(samples) is not None or len(samples) >= MAX_SAMPLES:
        return
    samples.append(label)
    controller["evidence"][key] = samples


def midpoint(low: int, high: int) -> int | None:
    """Whole-minute midpoint strictly inside (low, high). Floor of the mean.

    600..900 -> 720. Span <= 60 -> None (done).
    """
    if high - low <= MIN_BRACKET_SPAN:
        return None
    mid = ((low + high) // 2) // 60 * 60
    if mid <= low:
        mid = low + 60
    if mid >= high:
        mid = high - 60
    if mid <= low or mid >= high:
        return None
    return mid


def _highest_good_below(controller: dict[str, Any], high: int) -> int | None:
    best = None
    for key, labels in controller["evidence"].items():
        interval = int(key)
        if interval < high and "good" in labels:
            if best is None or interval > best:
                best = interval
    return best


def _finish(controller: dict[str, Any], sampling: dict[str, Any],
            last_measured: int) -> dict[str, Any]:
    low = controller["known_good_seconds"]
    high = controller["confirmed_bad_seconds"]
    controller["mode"] = MODE_DONE
    controller["candidate_seconds"] = None
    controller["result"] = {
        "recommended_seconds": low,
        "bracket_low_seconds": low,
        "bracket_high_seconds": high,
        "confidence": {
            "lower_samples": len(_labels(controller, low)) if low is not None else 0,
            "upper_samples": len(_labels(controller, high)) if high is not None else 0,
            "rule": "2-of-max-3",
        },
    }
    # Operating cadence after search: the confirmed-good lower bound.
    nxt = low if low is not None else sampling["next_interval_seconds"]
    return {
        "progress": sampling["progress"],
        "next_interval_seconds": nxt,
        "last_measured_interval_seconds": last_measured,
    }


def _highest_unconfirmed_good_below(controller: dict[str, Any], high: int) -> int | None:
    best = None
    for key, labels in controller["evidence"].items():
        interval = int(key)
        if interval >= high or "good" not in labels:
            continue
        if decision(list(labels)) == "good":
            continue
        if best is None or interval > best:
            best = interval
    return best


def _after_confirmed_bad(controller: dict[str, Any], sampling: dict[str, Any],
                         last_measured: int) -> dict[str, Any]:
    high = controller["confirmed_bad_seconds"]
    pending = _highest_unconfirmed_good_below(controller, high) if high is not None else None
    if pending is not None:
        controller["mode"] = MODE_CONFIRM
        controller["candidate_seconds"] = pending
        return {
            "progress": sampling["progress"],
            "next_interval_seconds": pending,
            "last_measured_interval_seconds": last_measured,
        }
    low = controller["known_good_seconds"]
    if low is not None and high is not None:
        mid = midpoint(low, high)
        if mid is None:
            return _finish(controller, sampling, last_measured)
        controller["mode"] = MODE_BRACKET
        controller["candidate_seconds"] = mid
        return {
            "progress": sampling["progress"],
            "next_interval_seconds": mid,
            "last_measured_interval_seconds": last_measured,
        }
    below = _highest_good_below(controller, high) if high is not None else None
    if below is None:
        return _finish(controller, sampling, last_measured)
    controller["known_good_seconds"] = below
    return _after_confirmed_bad(controller, sampling, last_measured)


def apply(sampling: dict[str, Any], controller: dict[str, Any], *,
          interval: int | None, classification: str, valid: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update controller + sampling handoff after one measurement.

    Returns (controller, sampling_updates) where sampling_updates always
    contains next_interval_seconds (authoritative planned interval).
    """
    controller = ensure_controller(controller)
    progress = int(sampling["progress"])
    current_next = sampling["next_interval_seconds"]
    last_measured = sampling.get("last_measured_interval_seconds")
    freeze = {
        "progress": progress,
        "next_interval_seconds": current_next,
        "last_measured_interval_seconds": last_measured,
    }

    if interval is None:
        from .core import VIRGIN_INTERVALS
        return controller, {
            "progress": progress,
            "next_interval_seconds": VIRGIN_INTERVALS[0],
            "last_measured_interval_seconds": None,
        }

    label = _evidence_label(classification)
    if not valid or label is None:
        return controller, freeze

    _add_sample(controller, interval, label)
    decided = decision(_labels(controller, interval))
    last_measured = interval

    if controller["mode"] == MODE_DONE:
        recommended = (controller.get("result") or {}).get("recommended_seconds")
        return controller, {
            "progress": progress,
            "next_interval_seconds": recommended if recommended is not None else interval,
            "last_measured_interval_seconds": last_measured,
        }

    if controller["mode"] == MODE_EXPLORE:
        if label == "good":
            if decided == "good":
                controller["known_good_seconds"] = (
                    interval if controller["known_good_seconds"] is None
                    else max(controller["known_good_seconds"], interval)
                )
            progress += 1
            return controller, {
                "progress": progress,
                "next_interval_seconds": _explorer_next(progress, interval),
                "last_measured_interval_seconds": last_measured,
            }
        controller["mode"] = MODE_CONFIRM
        controller["candidate_seconds"] = interval
        if decided == "bad":
            controller["confirmed_bad_seconds"] = interval
            return controller, _after_confirmed_bad(controller, sampling, last_measured)
        return controller, {
            "progress": progress,
            "next_interval_seconds": interval,
            "last_measured_interval_seconds": last_measured,
        }

    if controller["mode"] == MODE_CONFIRM:
        if decided is None:
            return controller, {
                "progress": progress,
                "next_interval_seconds": interval,
                "last_measured_interval_seconds": last_measured,
            }
        controller["candidate_seconds"] = None
        if decided == "good":
            controller["known_good_seconds"] = (
                interval if controller["known_good_seconds"] is None
                else max(controller["known_good_seconds"], interval)
            )
            high = controller["confirmed_bad_seconds"]
            if high is not None:
                return controller, _after_confirmed_bad(controller, sampling, last_measured)
            controller["mode"] = MODE_EXPLORE
            progress += 1
            return controller, {
                "progress": progress,
                "next_interval_seconds": _explorer_next(progress, interval),
                "last_measured_interval_seconds": last_measured,
            }
        controller["confirmed_bad_seconds"] = (
            interval if controller["confirmed_bad_seconds"] is None
            else min(controller["confirmed_bad_seconds"], interval)
        )
        return controller, _after_confirmed_bad(controller, sampling, last_measured)

    # bracket
    if decided is None:
        return controller, {
            "progress": progress,
            "next_interval_seconds": interval,
            "last_measured_interval_seconds": last_measured,
        }
    if decided == "good":
        controller["known_good_seconds"] = interval
    else:
        controller["confirmed_bad_seconds"] = interval
    return controller, _after_confirmed_bad(controller, sampling, last_measured)
