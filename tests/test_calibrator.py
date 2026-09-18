import datetime as dt
import tempfile
import unittest
from pathlib import Path

import io
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from agentstatus.calibrator import cli
from agentstatus.calibrator.core import Calibrator, VIRGIN_INTERVALS
from agentstatus.calibrator.model import ActivityResult, Assessment, Observation
from agentstatus.calibrator.persistence import load_histories, load_history, load_state
from agentstatus.calibrator.render import (
    chronological, interval_label, render_header, render_record, render_wait_status,
)


UTC = dt.timezone.utc


class Clock:
    def __init__(self):
        self.value = dt.datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += dt.timedelta(seconds=seconds)


class FakeAdapter:
    provider = "test-provider"
    agent = "test-agent"
    display_name = "TEST"
    measurement_schema = "test/v1"
    display_columns = (("count", "COUNT"),)

    def __init__(self, clock, validity=None, classifications=None, update_baseline=None):
        self.clock = clock
        self.validity = iter(validity or [])
        self.classifications = iter(classifications or [])
        self.update_baseline = iter(update_baseline or [])
        self.probes = 0
    def detect_activity(self):
        return ActivityResult("activity", self.clock.value - dt.timedelta(days=1), reliable_checks=1,
                              source="fixture")
    def probe(self):
        self.probes += 1
        self.clock.value += dt.timedelta(seconds=7)
        valid = next(self.validity, True)
        return Observation(valid, f"m-{self.probes}", {"count": self.probes}, {"count": self.probes}, None if valid else "failed")
    def assess(self, observation, baseline, *, startup=False):
        if not observation.valid:
            return Assessment("fail", baseline, update_baseline=False)
        classification = next(self.classifications, "good")
        update = next(self.update_baseline, classification == "good")
        return Assessment(classification, {"seen": self.probes} if update else dict(baseline), update)


class ScriptedActivityAdapter(FakeAdapter):
    """FakeAdapter whose detect_activity() replays a fixed script."""

    def __init__(self, clock, script):
        super().__init__(clock)
        self.script = iter(script)
    def detect_activity(self):
        return next(self.script)


class CalibratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "states/test.json"
        self.history = root / "history.jsonl"
        self.clock = Clock()
        self.adapter = FakeAdapter(self.clock)
    def tearDown(self):
        self.temp.cleanup()

    def runner(self, adapter=None, check_interval=30):
        return Calibrator(adapter or self.adapter, self.state, self.history, clock=self.clock,
                          sleeper=self.clock.sleep, check_interval=check_interval)

    def measurements(self):
        return [r for r in load_history(self.history) if r["event"] == "measurement"]

    def test_virgin_startup_and_linear_sampling_sequence(self):
        self.assertEqual(self.runner().run(max_measurements=8), 0)
        records = self.measurements()
        self.assertIsNone(records[0]["interval_seconds"])
        self.assertEqual([r["interval_seconds"] for r in records[1:]],
                         [60, 60, 120, 180, 240, 300, 600, 900])
        self.assertEqual([interval_label(record) for record in records], [
            "startup", "1m·", "1m↑1m", "2m↑1m", "3m↑1m", "4m↑1m",
            "5m↑5m", "10m↑5m", "15m↑5m",
        ])
        self.assertEqual([record["next_interval_seconds"] for record in records],
                         [60, 60, 120, 180, 240, 300, 600, 900, 1200])

    def test_restart_between_two_one_minute_measurements(self):
        self.runner().run(max_measurements=1)
        first_state = load_state(self.state)
        self.assertEqual(first_state["sampling"]["progress"], 1)
        self.assertEqual(first_state["sampling"]["next_interval_seconds"], 60)
        self.runner().run(max_measurements=1)
        records = self.measurements()
        self.assertEqual([r["interval_seconds"] for r in records], [None, 60, 60])
        self.assertEqual([interval_label(r) for r in records], ["startup", "1m·", "1m↑1m"])
        self.assertEqual(records[-1]["next_interval_seconds"], 120)

    def test_resume_later_does_not_restart_virgin_sequence(self):
        self.runner().run(max_measurements=6)
        self.runner().run(max_measurements=2)
        self.assertEqual([r["interval_seconds"] for r in self.measurements()[-2:]], [600, 900])
        self.assertEqual(sum(r["interval_seconds"] is None for r in self.measurements()), 1)

    def test_invalid_measurement_does_not_advance_progress(self):
        self.adapter = FakeAdapter(self.clock, [True, False])
        self.assertEqual(self.runner().run(max_measurements=1), 1)
        state = load_state(self.state)
        self.assertEqual(state["sampling"]["progress"], 0)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 60)
        failed = self.measurements()[-1]
        self.assertIsNone(failed["next_interval_seconds"])
        self.assertIsNone(failed["next_movement_seconds"])
        self.assertIsNone(failed["next_scheduled_at"])

    def test_next_is_computed_from_probe_end(self):
        shown = []
        self.runner().run(max_measurements=1, emit=shown.append)
        first = shown[0]
        ended = dt.datetime.fromisoformat(first["timestamp"].replace("Z", "+00:00"))
        next_at = dt.datetime.fromisoformat(first["next_scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual((next_at - ended).total_seconds(), first["next_interval_seconds"])

    def test_every_next_deadline_uses_the_persisted_next_interval(self):
        self.runner().run(max_measurements=8)
        for record in self.measurements():
            ended = dt.datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            next_at = dt.datetime.fromisoformat(record["next_scheduled_at"].replace("Z", "+00:00"))
            self.assertEqual((next_at - ended).total_seconds(), record["next_interval_seconds"])
        state = load_state(self.state)
        self.assertEqual(state["sampling"]["next_interval_seconds"],
                         self.measurements()[-1]["next_interval_seconds"])

    def test_movement_rendering(self):
        # Records with the additive prospective field use the new contract.
        self.assertEqual(interval_label({"interval_seconds": 60, "movement_seconds": 0,
                                         "next_movement_seconds": 60}), "1m↑1m")
        # Legacy records retain their original retrospective interpretation.
        self.assertEqual(interval_label({"interval_seconds": 60, "movement_seconds": 0}), "1m·")
        self.assertEqual(interval_label({"interval_seconds": 120, "movement_seconds": 60}), "2m↑1m")
        self.assertEqual(interval_label({"interval_seconds": 3300, "movement_seconds": -300}), "55m↓5m")

    def test_uniform_header_and_legacy_history_rendering(self):
        self.assertEqual(render_header(),
            "TIME      AGENT    INTERVAL  C.READ    C.WRITE   IN        OUT       TOTAL     COST      NEXT")
        legacy = {"timestamp": "2026-09-18T06:00:00Z", "interval_seconds": 60,
                  "movement_seconds": 0,
                  "display": {"read": 8, "create": 2, "total": 10, "cost": "$0.0010"}}
        line = render_record(legacy, "CLAUDE")
        self.assertIn("1m·", line)
        self.assertIn("8         2         -         -         10        $0.0010", line)

    def test_history_is_chronological_and_identified(self):
        self.runner().run(max_measurements=2)
        records = self.measurements()
        self.assertEqual(records, chronological(reversed(records)))
        for record in records:
            self.assertEqual(record["agent"], "test-agent")
            self.assertEqual(record["provider"], "test-provider")
            self.assertEqual(record["measurement_schema"], "test/v1")
            self.assertTrue(record["measurement_id"])

    def test_per_agent_state_isolation(self):
        self.runner().run(max_measurements=1)
        other = FakeAdapter(self.clock)
        other.agent = "other-agent"
        other_state = self.state.with_name("other.json")
        Calibrator(other, other_state, self.history, clock=self.clock,
                   sleeper=self.clock.sleep).run(max_measurements=2)
        self.assertEqual(load_state(self.state)["sampling"]["progress"], 1)
        self.assertEqual(load_state(other_state)["sampling"]["progress"], 2)

    def test_two_calibrator_instances_do_not_mix_state_or_history(self):
        """Two agents run through two independent Calibrator instances, each
        with its own state and history file (per-agent, no shared writer)."""
        other = FakeAdapter(self.clock)
        other.agent = "other-agent"
        other.provider = "other-provider"
        other.measurement_schema = "other/v1"
        other_state = self.state.with_name("other.json")
        other_history = self.history.with_name("other-history.jsonl")

        self.runner().run(max_measurements=2)
        Calibrator(other, other_state, other_history, clock=self.clock,
                   sleeper=self.clock.sleep).run(max_measurements=3)

        mine = self.measurements()
        theirs = [r for r in load_history(other_history) if r["event"] == "measurement"]
        # Each run(max_measurements=N) records the startup measurement plus
        # N interval measurements.
        self.assertEqual(len(mine), 3)
        self.assertEqual(len(theirs), 4)
        self.assertTrue(all(r["agent"] == "test-agent" for r in mine))
        self.assertTrue(all(r["agent"] == "other-agent" for r in theirs))
        # Each agent's own file never contains the other agent's records.
        self.assertFalse(any(r["agent"] == "other-agent" for r in mine))
        self.assertFalse(any(r["agent"] == "test-agent" for r in theirs))
        # State never crosses over either.
        self.assertEqual(load_state(self.state)["agent"], "test-agent")
        self.assertEqual(load_state(other_state)["agent"], "other-agent")

    def test_per_agent_histories_merge_chronologically(self):
        other = FakeAdapter(self.clock)
        other.agent = "other-agent"
        other_state = self.state.with_name("other.json")
        other_history = self.history.with_name("other-history.jsonl")

        self.runner().run(max_measurements=2)
        Calibrator(other, other_state, other_history, clock=self.clock,
                   sleeper=self.clock.sleep).run(max_measurements=2)

        merged = chronological(load_histories([self.history, other_history]))
        timestamps = [r["timestamp"] for r in merged]
        self.assertEqual(timestamps, sorted(timestamps))
        agents = {r["agent"] for r in merged}
        self.assertEqual(agents, {"test-agent", "other-agent"})

    def test_controller_state_is_reserved_and_left_untouched(self):
        self.runner().run(max_measurements=1)
        state = load_state(self.state)
        self.assertIn("controller", state)
        self.assertIn("schema", state["controller"])
        before = dict(state["controller"])
        self.runner().run(max_measurements=1)
        after = load_state(self.state)["controller"]
        self.assertEqual(before, after)

    # -- baseline contract (generic: the core only ever respects the flag) --
    #
    # FakeAdapter.assess() is called for the startup measurement too (every
    # run(max_measurements=1) call performs startup *and* the first interval
    # measurement in one call, since startup itself doesn't count towards
    # max_measurements). Scripts below always cover startup first, with
    # update_baseline=False for it, matching "startup only updates the
    # baseline when explicitly told to" -- exercised precisely in
    # test_calibrator_claude.py.

    def test_good_classification_advances_ladder(self):
        self.adapter = FakeAdapter(self.clock, classifications=["good", "good"],
                                   update_baseline=[False, True])
        self.runner().run(max_measurements=1)
        self.assertEqual(load_state(self.state)["sampling"]["progress"], 1)

    def test_bad_classification_advances_ladder_without_updating_baseline(self):
        self.adapter = FakeAdapter(self.clock, classifications=["good", "bad"],
                                   update_baseline=[False, False])
        self.runner().run(max_measurements=1)
        state = load_state(self.state)
        self.assertEqual(state["sampling"]["progress"], 1)
        self.assertEqual(state["baseline"], {})

    def test_init_classification_advances_ladder_without_updating_baseline(self):
        self.adapter = FakeAdapter(self.clock, classifications=["good", "init"],
                                   update_baseline=[False, False])
        self.runner().run(max_measurements=1)
        state = load_state(self.state)
        self.assertEqual(state["sampling"]["progress"], 1)
        self.assertEqual(state["baseline"], {})

    def test_fail_does_not_advance_ladder(self):
        self.adapter = FakeAdapter(self.clock, validity=[True, False])
        self.assertEqual(self.runner().run(max_measurements=1), 1)
        state = load_state(self.state)
        self.assertEqual(state["sampling"]["progress"], 0)

    def test_invalid_does_not_advance_ladder(self):
        self.adapter = FakeAdapter(self.clock, validity=[True, False], update_baseline=[False])
        self.assertEqual(self.runner().run(max_measurements=1), 1)
        state = load_state(self.state)
        self.assertEqual(state["sampling"]["progress"], 0)
        self.assertEqual(state["baseline"], {})

    def test_core_only_writes_baseline_when_assessment_flags_update(self):
        self.adapter = FakeAdapter(self.clock, classifications=["good", "good", "bad"],
                                   update_baseline=[False, True, False])
        self.runner().run(max_measurements=2)
        # The first (startup) measurement never updates (flagged False). The
        # following "good" interval measurement updates the baseline; the
        # "bad" one after it must not overwrite it.
        state = load_state(self.state)
        self.assertEqual(state["baseline"], {"seen": 2})

    # -- activity model / scheduler semantics --

    def test_activity_reschedules_deadline(self):
        early = ActivityResult("activity", self.clock.value - dt.timedelta(seconds=200), reliable_checks=1)
        later = ActivityResult("activity", self.clock.value + dt.timedelta(seconds=500), reliable_checks=1)
        # First measurement is the free startup sample; script covers the
        # following interval-based wait: initial check, then a rescheduling
        # tick, then enough "later" checks to actually reach the deadline.
        script = [early] + [later] * 40
        self.adapter = ScriptedActivityAdapter(self.clock, script)
        shown = []
        self.runner(check_interval=30).run(max_measurements=1, emit=shown.append)
        events = load_history(self.history)
        self.assertTrue(any(e["event"] == "activity-detected" for e in events))
        second = shown[1]
        finished = dt.datetime.fromisoformat(second["timestamp"].replace("Z", "+00:00"))
        # The probe could only fire once the clock passed the rescheduled
        # deadline (later.at + 60s), well past the naive early.at + 60s.
        self.assertGreaterEqual(finished, later.at)

    def test_reliable_none_uses_probe_floor_not_epoch_or_idle(self):
        script = [ActivityResult("none", reliable_checks=2)] * 10
        self.adapter = ScriptedActivityAdapter(self.clock, script)
        start = self.clock.value
        shown = []
        self.runner(check_interval=30).run(max_measurements=2, emit=shown.append)
        second = shown[1]
        finished = dt.datetime.fromisoformat(second["timestamp"].replace("Z", "+00:00"))
        first_finished = dt.datetime.fromisoformat(shown[0]["timestamp"].replace("Z", "+00:00"))
        # Anchored on the probe-floor (the first measurement's finish time),
        # not on epoch (which would already be far in the past and cause an
        # immediate probe) and not on any invented "idle" timestamp.
        self.assertGreaterEqual(finished, first_finished)
        self.assertGreater(finished, start)
        events = load_history(self.history)
        self.assertFalse(any(e["event"] == "activity-unreliable" for e in events))
        self.assertFalse(any(e["event"] == "activity-detected" for e in events))

    def test_unreliable_activity_is_never_treated_as_none(self):
        script = [ActivityResult("unreliable", failed_checks=("tool-x: boom",))] * 10
        self.adapter = ScriptedActivityAdapter(self.clock, script)
        first_finish_floor = None
        shown = []
        rc = self.runner(check_interval=30).run(max_measurements=2, emit=shown.append)
        self.assertEqual(rc, 0)
        events = load_history(self.history)
        unreliable_events = [e for e in events if e["event"] == "activity-unreliable"]
        self.assertTrue(unreliable_events, "unreliable checks must be recorded, distinctly from none")
        self.assertEqual(unreliable_events[0]["failed_checks"], ["tool-x: boom"])
        # Fail-closed: no reschedule event, and the probe still only fires
        # at (or after) probe-floor + interval -- never immediately/early.
        self.assertFalse(any(e["event"] == "activity-detected" for e in events))
        first_finished = dt.datetime.fromisoformat(shown[0]["timestamp"].replace("Z", "+00:00"))
        second_finished = dt.datetime.fromisoformat(shown[1]["timestamp"].replace("Z", "+00:00"))
        self.assertGreaterEqual((second_finished - first_finished).total_seconds(), 60)

    def test_probe_floor_falls_back_to_now_not_epoch_when_unset(self):
        calibrator = self.runner()
        self.assertEqual(calibrator._probe_floor({"last_probe_finished_at": None}), self.clock())

    def test_unreliable_does_not_cause_unwarranted_immediate_probe(self):
        # Craft a resumed state that is past startup but has no persisted
        # probe-floor -- an edge case a real run never produces (startup
        # always sets it first), exercised directly here to prove the
        # "no floor" fallback can't be exploited into an unwarranted probe.
        from agentstatus.calibrator.core import initial_state
        from agentstatus.calibrator.persistence import save_state
        state = initial_state(self.adapter)
        state["sampling"]["startup_complete"] = True
        state["sampling"]["next_interval_seconds"] = 60
        save_state(self.state, state)
        self.adapter = ScriptedActivityAdapter(
            self.clock, [ActivityResult("unreliable", failed_checks=("x",))] * 5)
        start = self.clock()
        shown = []
        self.runner(check_interval=30).run(max_measurements=1, emit=shown.append)
        finished = dt.datetime.fromisoformat(shown[0]["timestamp"].replace("Z", "+00:00"))
        self.assertGreaterEqual((finished - start).total_seconds(), 60)

    def test_wait_status_appears_before_sleeper_and_interval_probe(self):
        self.adapter = ScriptedActivityAdapter(self.clock, [ActivityResult("none")] * 30)
        waits = []
        sleeps = []
        def sleeper(seconds):
            sleeps.append((self.adapter.probes, seconds))
            self.clock.sleep(seconds)
        Calibrator(self.adapter, self.state, self.history, clock=self.clock,
                   sleeper=sleeper, check_interval=30).run(
            max_measurements=1, on_wait=waits.append)
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["event"], "wait-status")
        self.assertEqual(waits[0]["reason"], "initial")
        self.assertEqual(waits[0]["interval_seconds"], 60)
        self.assertEqual(sleeps[0][0], 1)
        self.assertEqual(self.adapter.probes, 2)
        self.assertFalse(any(r["event"] == "wait-status" for r in load_history(self.history)))

    def test_wait_status_initial_deadline_uses_probe_floor(self):
        waits = []
        self.runner().run(max_measurements=1, on_wait=waits.append)
        finished = dt.datetime.fromisoformat(self.measurements()[0]["timestamp"].replace("Z", "+00:00"))
        scheduled = dt.datetime.fromisoformat(waits[0]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual((scheduled - finished).total_seconds(), 60)

    def test_recent_activity_shifts_initial_wait_deadline(self):
        activity_at = dt.datetime(2026, 9, 18, 6, 0, 20, tzinfo=UTC)
        self.adapter = ScriptedActivityAdapter(
            self.clock, [ActivityResult("activity", activity_at, reliable_checks=1, source="live")] * 20)
        waits = []
        self.runner().run(max_measurements=1, on_wait=waits.append)
        scheduled = dt.datetime.fromisoformat(waits[0]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(scheduled, activity_at + dt.timedelta(seconds=60))

    def test_newer_activity_during_wait_emits_reschedule(self):
        first = dt.datetime(2026, 9, 18, 6, 0, 10, tzinfo=UTC)
        later = dt.datetime(2026, 9, 18, 6, 0, 40, tzinfo=UTC)
        self.adapter = ScriptedActivityAdapter(self.clock, [
            ActivityResult("activity", first, reliable_checks=1, source="a"),
            ActivityResult("activity", later, reliable_checks=1, source="b"),
        ] + [ActivityResult("none")] * 20)
        waits = []
        self.runner(check_interval=30).run(max_measurements=1, on_wait=waits.append)
        self.assertGreaterEqual(len(waits), 2)
        self.assertEqual(waits[0]["reason"], "initial")
        self.assertEqual(waits[1]["reason"], "rescheduled")
        scheduled = dt.datetime.fromisoformat(waits[1]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(scheduled, later + dt.timedelta(seconds=60))

    def test_unreliable_activity_does_not_shorten_wait_deadline(self):
        waits = []
        self.adapter = ScriptedActivityAdapter(self.clock, [
            ActivityResult("none"),
            ActivityResult("unreliable", failed_checks=("x",)),
        ] + [ActivityResult("none")] * 20)
        self.runner(check_interval=30).run(max_measurements=1, on_wait=waits.append)
        finished = dt.datetime.fromisoformat(self.measurements()[0]["timestamp"].replace("Z", "+00:00"))
        scheduled = dt.datetime.fromisoformat(waits[0]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual((scheduled - finished).total_seconds(), 60)
        self.assertEqual([w["reason"] for w in waits], ["initial"])

    def test_startup_does_not_emit_interval_wait_status(self):
        waits = []
        probes_at_wait = []
        def on_wait(status):
            probes_at_wait.append(self.adapter.probes)
            waits.append(status)
        self.runner().run(max_measurements=1, on_wait=on_wait)
        self.assertTrue(all(count >= 1 for count in probes_at_wait))
        self.assertTrue(all(w["interval_seconds"] == 60 for w in waits))

    def test_resume_ignores_expired_persisted_next_scheduled_at(self):
        from agentstatus.calibrator.core import initial_state, iso
        from agentstatus.calibrator.persistence import save_state
        state = initial_state(self.adapter)
        state["sampling"]["startup_complete"] = True
        state["sampling"]["progress"] = 1
        state["sampling"]["next_interval_seconds"] = 60
        state["sampling"]["last_measured_interval_seconds"] = 60
        state["sampling"]["last_probe_finished_at"] = iso(self.clock())
        save_state(self.state, state)
        from agentstatus.calibrator.persistence import append_history
        append_history(self.history, {
            "event": "measurement", "timestamp": iso(self.clock()),
            "agent": "test-agent", "provider": "test-provider",
            "measurement_schema": "test/v1", "interval_seconds": 60,
            "next_scheduled_at": "2026-09-18T05:00:00Z",
        })
        self.adapter = ScriptedActivityAdapter(self.clock, [ActivityResult("none")] * 30)
        waits = []
        self.runner().run(max_measurements=1, on_wait=waits.append)
        scheduled = dt.datetime.fromisoformat(waits[0]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(scheduled, dt.datetime(2026, 9, 18, 6, 1, tzinfo=UTC))
        self.assertNotEqual(waits[0]["scheduled_at"], "2026-09-18T05:00:00Z")

    def test_wait_status_rendering_does_not_reuse_measurement_metrics(self):
        now = dt.datetime(2026, 9, 18, 6, 0, 7, tzinfo=UTC)
        deadline = dt.datetime(2026, 9, 18, 6, 1, 7, tzinfo=UTC)
        line = render_wait_status("TEST", 60, now, deadline)
        self.assertIn("1m", line)
        self.assertIn("-", line)
        self.assertNotIn("$", line)
        measured = render_record({
            "timestamp": "2026-09-18T06:00:07Z",
            "interval_seconds": 60,
            "next_movement_seconds": 0,
            "display": {"c_read": 20, "c_write": 4, "input": 30, "output": 6, "total": 36, "cost": "-"},
        }, "TEST", deadline)
        self.assertIn("20", measured)
        self.assertNotEqual(line, measured)


class ScratchDirCliTests(unittest.TestCase):
    def test_codex_and_grok_share_the_same_scratch_dir_option(self):
        fake = Mock(display_name="X")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "CodexCalibratorAdapter", return_value=fake) as codex, \
             patch.object(cli, "GrokCalibratorAdapter", return_value=fake) as grok, \
             redirect_stdout(io.StringIO()):
            scratch = Path(directory) / "scratch"
            common = ["status", "--scratch-dir", str(scratch), "--state-dir", directory]
            self.assertEqual(cli.main([*common, "--agent", "codex", "--model", "fixture"]), 0)
            self.assertEqual(cli.main([*common, "--agent", "grok"]), 0)
        self.assertEqual(codex.call_args.kwargs["scratch_root"], scratch)
        self.assertEqual(grok.call_args.kwargs["scratch_root"], scratch)
        source = __import__("inspect").getsource(cli.main)
        self.assertIn("--scratch-dir", source)
        self.assertIn("Codex and Grok", source)
        self.assertLess(source.index("--scratch-dir"), source.index("Codex options"))


if __name__ == "__main__":
    unittest.main()
