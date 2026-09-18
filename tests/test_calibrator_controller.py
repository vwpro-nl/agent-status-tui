import datetime as dt
import tempfile
import unittest
from pathlib import Path

from agentstatus.calibrator.controller import (
    MODE_BRACKET, MODE_CONFIRM, MODE_DONE, MODE_EXPLORE, apply, decision,
    empty_controller, midpoint,
)
from agentstatus.calibrator.core import Calibrator, initial_state
from agentstatus.calibrator.model import Assessment, Observation
from agentstatus.calibrator.persistence import append_history, load_history, load_state, save_state
from tests.test_calibrator import Clock, FakeAdapter


LIVE = (
    (60, "good"),
    (60, "bad"),
    (120, "good"),
    (180, "bad"),
    (240, "bad"),
    (300, "good"),
    (600, "good"),
    (900, "bad"),
)


def _sampling(**overrides):
    sampling = initial_state(FakeAdapter(Clock()))["sampling"]
    sampling.update(overrides)
    return sampling


class ConfirmationUnitTests(unittest.TestCase):
    def test_decision_two_of_max_three(self):
        self.assertIsNone(decision(["bad"]))
        self.assertIsNone(decision(["good"]))
        self.assertIsNone(decision(["bad", "good"]))
        self.assertEqual(decision(["bad", "bad"]), "bad")
        self.assertEqual(decision(["good", "good"]), "good")
        self.assertEqual(decision(["bad", "good", "bad"]), "bad")
        self.assertEqual(decision(["bad", "good", "good"]), "good")

    def test_midpoint_floors_to_whole_minutes_strictly_inside(self):
        self.assertEqual(midpoint(600, 900), 720)
        self.assertIsNone(midpoint(600, 660))
        self.assertEqual(midpoint(600, 720), 660)


class ControllerApplyTests(unittest.TestCase):
    def observe(self, sampling, controller, interval, classification, valid=True):
        return apply(sampling, controller, interval=interval,
                     classification=classification, valid=valid)

    def test_first_15m_bad_repeats_15m_not_20m(self):
        sampling = _sampling(startup_complete=True, progress=7,
                             next_interval_seconds=900, last_measured_interval_seconds=600)
        controller = empty_controller()
        for interval, label in LIVE[:-1]:
            apply(sampling, controller, interval=interval, classification=label, valid=True)
            # Historical explorer already reached 15m; only record evidence.
            controller["mode"] = MODE_EXPLORE
        sampling["progress"] = 7
        sampling["next_interval_seconds"] = 900
        sampling["last_measured_interval_seconds"] = 600
        controller["mode"] = MODE_EXPLORE
        controller, updates = self.observe(sampling, controller, 900, "bad")
        self.assertEqual(controller["mode"], MODE_CONFIRM)
        self.assertEqual(updates["next_interval_seconds"], 900)
        self.assertEqual(updates["progress"], 7)
        self.assertNotEqual(updates["next_interval_seconds"], 1200)

    def test_case_a_confirmed_15m_bad_confirms_10m_before_bracket(self):
        sampling = _sampling(startup_complete=True, progress=7,
                             next_interval_seconds=900, last_measured_interval_seconds=600)
        controller = empty_controller()
        for interval, label in LIVE:
            controller, updates = self.observe(sampling, controller, interval, label)
            sampling.update(updates)
        # Sequential apply stops at 1m bad. Seed explore-at-15m then two 15m bads.
        sampling = _sampling(startup_complete=True, progress=7,
                             next_interval_seconds=900, last_measured_interval_seconds=600)
        controller = empty_controller()
        for interval, label in LIVE[:-1]:
            apply(sampling, controller, interval=interval, classification=label, valid=True)
        controller["mode"] = MODE_EXPLORE
        sampling["progress"] = 7
        sampling["next_interval_seconds"] = 900
        controller, updates = self.observe(sampling, controller, 900, "bad")
        sampling.update(updates)
        controller, updates = self.observe(sampling, controller, 900, "bad")
        self.assertEqual(controller["mode"], MODE_CONFIRM)
        self.assertEqual(controller["confirmed_bad_seconds"], 900)
        self.assertEqual(updates["next_interval_seconds"], 600)
        self.assertEqual(controller["candidate_seconds"], 600)

    def test_case_b_15m_good_after_bad_then_good_resumes_explorer_at_20m(self):
        sampling = _sampling(startup_complete=True, progress=7,
                             next_interval_seconds=900, last_measured_interval_seconds=600)
        controller = empty_controller()
        controller, updates = self.observe(sampling, controller, 900, "bad")
        sampling.update(updates)
        self.assertEqual(controller["mode"], MODE_CONFIRM)
        controller, updates = self.observe(sampling, controller, 900, "good")
        sampling.update(updates)
        self.assertEqual(updates["next_interval_seconds"], 900)
        controller, updates = self.observe(sampling, controller, 900, "good")
        self.assertEqual(controller["mode"], MODE_EXPLORE)
        self.assertEqual(updates["next_interval_seconds"], 1200)
        self.assertEqual(updates["progress"], 8)

    def test_case_c_midpoint_needs_two_samples(self):
        sampling = _sampling(startup_complete=True, progress=7, next_interval_seconds=720)
        controller = empty_controller()
        controller["mode"] = MODE_BRACKET
        controller["known_good_seconds"] = 600
        controller["confirmed_bad_seconds"] = 900
        controller["candidate_seconds"] = 720
        controller["evidence"] = {"600": ["good", "good"], "900": ["bad", "bad"]}
        controller, updates = self.observe(sampling, controller, 720, "bad")
        self.assertEqual(controller["mode"], MODE_BRACKET)
        self.assertEqual(updates["next_interval_seconds"], 720)
        controller, updates = self.observe(sampling, controller, 720, "bad")
        self.assertEqual(controller["confirmed_bad_seconds"], 720)
        self.assertNotEqual(updates["next_interval_seconds"], 720)

    def test_case_d_init_fail_invalid_do_not_change_evidence(self):
        sampling = _sampling(startup_complete=True, progress=7, next_interval_seconds=900)
        controller = empty_controller()
        controller, updates = self.observe(sampling, controller, 900, "bad")
        sampling.update(updates)
        before = dict(controller["evidence"])
        for classification, valid in (("init", True), ("fail", False), ("bad", False)):
            controller, updates = self.observe(sampling, controller, 900, classification, valid=valid)
            self.assertEqual(controller["evidence"], before)
            self.assertEqual(updates["next_interval_seconds"], 900)
            self.assertEqual(controller["mode"], MODE_CONFIRM)

    def test_case_e_resume_keeps_confirm_interval(self):
        sampling = _sampling(startup_complete=True, progress=7, next_interval_seconds=900)
        controller = empty_controller()
        controller, updates = self.observe(sampling, controller, 900, "bad")
        sampling.update(updates)
        snapshot = (controller["mode"], updates["next_interval_seconds"], updates["progress"])
        controller, updates = self.observe(sampling, controller, 900, "init", valid=True)
        self.assertEqual(
            (controller["mode"], updates["next_interval_seconds"], updates["progress"]),
            snapshot,
        )


class ClassificationContractTests(unittest.TestCase):
    """Regression: adapters without boundary-crossing detection (Codex, Grok)
    report every valid, non-failing measurement as classification="ok" (see
    their own assess() tests). The controller must treat that as good
    evidence -- not silently freeze on it forever -- and the normalization
    must be keyed on the classification string, not on adapter identity, so
    it covers any adapter using this vocabulary.
    """

    def observe(self, sampling, controller, interval, classification, valid=True):
        return apply(sampling, controller, interval=interval,
                     classification=classification, valid=valid)

    def test_ok_classification_builds_good_evidence_not_raw_string(self):
        sampling = _sampling(startup_complete=True, progress=2, next_interval_seconds=120)
        controller = empty_controller()
        controller, updates = self.observe(sampling, controller, 120, "ok")
        self.assertEqual(controller["evidence"]["120"], ["good"])

    def test_ok_classification_advances_explorer_instead_of_freezing(self):
        # Before the fix, an unrecognized classification froze sampling
        # outright: progress and next_interval_seconds echoed back unchanged,
        # so an unlimited calibrator kept re-probing the same interval
        # forever without ever building evidence.
        sampling = _sampling(startup_complete=True, progress=2, next_interval_seconds=120)
        controller = empty_controller()
        controller, updates = self.observe(sampling, controller, 120, "ok")
        self.assertEqual(updates["progress"], 3)
        self.assertEqual(updates["next_interval_seconds"], 180)
        self.assertNotEqual(updates["next_interval_seconds"], 120)

    def test_invalid_ok_measurement_still_builds_no_evidence(self):
        sampling = _sampling(startup_complete=True, progress=2, next_interval_seconds=120)
        controller = empty_controller()
        controller, updates = self.observe(sampling, controller, 120, "ok", valid=False)
        self.assertEqual(controller["evidence"], {})
        self.assertEqual(updates["progress"], 2)
        self.assertEqual(updates["next_interval_seconds"], 120)

    def test_fail_and_init_still_build_no_evidence_alongside_ok(self):
        sampling = _sampling(startup_complete=True, progress=2, next_interval_seconds=120)
        controller = empty_controller()
        for classification, valid in (("fail", False), ("init", True)):
            controller, updates = self.observe(sampling, controller, 120, classification, valid=valid)
            self.assertEqual(controller["evidence"], {})
            self.assertEqual(updates["next_interval_seconds"], 120)

    def test_two_of_max_three_decision_still_applies_to_ok_evidence(self):
        # Mirrors the Claude confirm-then-resume-explorer sequence, but the
        # confirming samples are "ok" (Codex/Grok's vocabulary) instead of
        # "good" -- 2-of-max-3 must decide identically either way.
        sampling = _sampling(startup_complete=True, progress=7, next_interval_seconds=900)
        controller = empty_controller()
        controller, updates = self.observe(sampling, controller, 900, "bad")
        sampling.update(updates)
        self.assertEqual(controller["mode"], MODE_CONFIRM)
        controller, updates = self.observe(sampling, controller, 900, "ok")
        sampling.update(updates)
        # 1 bad / 1 ok(=good): still a tie, needs a third sample.
        self.assertIsNone(decision(controller["evidence"]["900"]))
        self.assertEqual(updates["next_interval_seconds"], 900)
        controller, updates = self.observe(sampling, controller, 900, "ok")
        # 1 bad / 2 ok(=good): decided good, resumes the explorer.
        self.assertEqual(controller["mode"], MODE_EXPLORE)
        self.assertEqual(updates["progress"], 8)
        self.assertEqual(updates["next_interval_seconds"], 1200)

    def test_claude_good_bad_boundary_behaviour_is_unaffected(self):
        # Same LIVE sequence as ControllerApplyTests -- proves the "ok"
        # normalization did not weaken or alter Claude's own good/bad path.
        sampling = _sampling(startup_complete=True, progress=7,
                             next_interval_seconds=900, last_measured_interval_seconds=600)
        controller = empty_controller()
        for interval, label in LIVE[:-1]:
            apply(sampling, controller, interval=interval, classification=label, valid=True)
        controller["mode"] = MODE_EXPLORE
        sampling["progress"] = 7
        sampling["next_interval_seconds"] = 900
        controller, updates = self.observe(sampling, controller, 900, "bad")
        sampling.update(updates)
        controller, updates = self.observe(sampling, controller, 900, "bad")
        self.assertEqual(controller["mode"], MODE_CONFIRM)
        self.assertEqual(controller["confirmed_bad_seconds"], 900)
        self.assertEqual(updates["next_interval_seconds"], 600)


class RealAdapterClassificationContractTests(unittest.TestCase):
    """Feeds the *actual* Codex/Grok Adapter.assess() output through the
    controller, rather than assuming "ok" means what it looks like it means.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _assert_valid_assessment_is_good_evidence(self, adapter):
        assessment = adapter.assess(Observation(True, "m", {"total_tokens": 1}), {})
        self.assertEqual(assessment.classification, "ok")
        failed = adapter.assess(Observation(False, "m2", error="x"), {})
        self.assertEqual(failed.classification, "fail")

        sampling = _sampling(startup_complete=True, progress=2, next_interval_seconds=120)
        controller = empty_controller()
        controller, updates = apply(sampling, controller, interval=120,
                                    classification=assessment.classification, valid=True)
        self.assertEqual(controller["evidence"]["120"], ["good"])
        self.assertEqual(updates["progress"], 3)

    def test_codex_ok_assessment_is_good_evidence_to_the_controller(self):
        from agentstatus.calibrator.adapters.codex import CodexCalibratorAdapter
        adapter = CodexCalibratorAdapter(self.root / "codex", model="fixture")
        self._assert_valid_assessment_is_good_evidence(adapter)

    def test_grok_ok_assessment_is_good_evidence_to_the_controller(self):
        from agentstatus.calibrator.adapters.grok import GrokCalibratorAdapter
        home = self.root / "grok"
        home.mkdir()
        adapter = GrokCalibratorAdapter(home)
        self._assert_valid_assessment_is_good_evidence(adapter)


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "state.json"
        self.history = root / "history.jsonl"
        self.clock = Clock()
        self.adapter = FakeAdapter(self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def run_with(self, classifications, n):
        self.adapter = FakeAdapter(self.clock, classifications=classifications,
                                   update_baseline=[False] * 20)
        return Calibrator(self.adapter, self.state, self.history, clock=self.clock,
                          sleeper=self.clock.sleep).run(max_measurements=n)

    def test_all_good_explorer_still_reaches_15m_then_20m_plan(self):
        self.run_with(["good"] * 20, 8)
        state = load_state(self.state)
        self.assertEqual(state["controller"]["mode"], MODE_EXPLORE)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 1200)
        records = [r for r in __import__("agentstatus.calibrator.persistence", fromlist=["load_history"]).load_history(self.history) if r["event"] == "measurement"]
        self.assertEqual(records[-1]["interval_seconds"], 900)

    def test_ok_classification_run_builds_evidence_and_advances_like_good(self):
        # Same shape as test_all_good_explorer_still_reaches_15m_then_20m_plan,
        # but for Codex/Grok's "ok" vocabulary: proves the fix at the full
        # Calibrator.run() level, not just controller.apply() in isolation.
        self.run_with(["ok"] * 20, 8)
        state = load_state(self.state)
        self.assertEqual(state["controller"]["mode"], MODE_EXPLORE)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 1200)
        self.assertTrue(state["controller"]["evidence"], "evidence must not stay frozen empty")
        records = [r for r in load_history(self.history) if r["event"] == "measurement"]
        self.assertEqual(records[-1]["interval_seconds"], 900)
        # Display/history terminology is untouched: still the adapter's own
        # raw "ok" string, not the controller's internal "good" evidence label.
        self.assertTrue(all(r["classification"] == "ok" for r in records[1:]))

    def test_first_15m_bad_via_run_repeats_900(self):
        # startup + 7 goods (through 10m) + 15m bad
        self.run_with(["good"] * 8 + ["bad"], 8)
        state = load_state(self.state)
        self.assertEqual(state["controller"]["mode"], MODE_CONFIRM)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 900)
        self.assertEqual(state["sampling"]["last_measured_interval_seconds"], 900)
        self.assertEqual(state["sampling"]["progress"], 7)

    def test_measurement_records_wait_metadata_not_probe_to_probe(self):
        self.run_with(["good"] * 4, 1)
        from agentstatus.calibrator.persistence import load_history
        interval = [r for r in load_history(self.history) if r.get("interval_seconds") == 60][0]
        self.assertIn("wait_anchor_at", interval)
        self.assertIn("scheduled_wait_at", interval)
        self.assertIn("actual_silence_seconds", interval)
        self.assertGreaterEqual(interval["actual_silence_seconds"], 60)

    def test_confirm_fail_keeps_authoritative_next_in_record_and_state(self):
        self.run_with(["good"] * 8 + ["bad"], 8)
        before = load_state(self.state)
        self.assertEqual(before["controller"]["mode"], MODE_CONFIRM)
        self.assertEqual(before["sampling"]["next_interval_seconds"], 900)
        evidence = dict(before["controller"]["evidence"])
        progress = before["sampling"]["progress"]
        candidate = before["controller"]["candidate_seconds"]
        self.adapter = FakeAdapter(self.clock, validity=[False], update_baseline=[False])
        rc = Calibrator(self.adapter, self.state, self.history, clock=self.clock,
                        sleeper=self.clock.sleep).run(max_measurements=1)
        self.assertEqual(rc, 1)
        after = load_state(self.state)
        failed = [r for r in __import__("agentstatus.calibrator.persistence", fromlist=["load_history"]).load_history(self.history) if r["event"] == "measurement"][-1]
        self.assertFalse(failed["valid"])
        self.assertEqual(after["controller"]["evidence"], evidence)
        self.assertEqual(after["sampling"]["progress"], progress)
        self.assertEqual(after["controller"]["mode"], MODE_CONFIRM)
        self.assertEqual(after["controller"]["candidate_seconds"], candidate)
        self.assertEqual(failed["next_interval_seconds"], 900)
        self.assertEqual(after["sampling"]["next_interval_seconds"], 900)
        finished = dt.datetime.fromisoformat(failed["timestamp"].replace("Z", "+00:00"))
        nxt = dt.datetime.fromisoformat(failed["next_scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual((nxt - finished).total_seconds(), 900)

    def test_confirm_init_keeps_next_and_adds_no_evidence(self):
        self.run_with(["good"] * 8 + ["bad"], 8)
        before = load_state(self.state)
        evidence = dict(before["controller"]["evidence"])
        self.adapter = FakeAdapter(self.clock, classifications=["init"], update_baseline=[False])
        self.assertEqual(
            Calibrator(self.adapter, self.state, self.history, clock=self.clock,
                       sleeper=self.clock.sleep).run(max_measurements=1),
            0,
        )
        after = load_state(self.state)
        record = [r for r in __import__("agentstatus.calibrator.persistence", fromlist=["load_history"]).load_history(self.history) if r["event"] == "measurement"][-1]
        self.assertEqual(record["classification"], "init")
        self.assertEqual(after["controller"]["evidence"], evidence)
        self.assertEqual(record["next_interval_seconds"], 900)
        self.assertEqual(after["sampling"]["next_interval_seconds"], 900)
        self.assertEqual(after["controller"]["mode"], MODE_CONFIRM)

    def test_resume_during_confirm(self):
        self.run_with(["good"] * 8 + ["bad"], 8)
        before = load_state(self.state)
        self.run_with(["bad"], 1)
        after = load_state(self.state)
        self.assertEqual(before["sampling"]["next_interval_seconds"], 900)
        self.assertEqual(after["controller"]["mode"], MODE_CONFIRM)
        self.assertEqual(after["sampling"]["next_interval_seconds"], 600)
        self.assertEqual(after["controller"]["candidate_seconds"], 600)


class RunTerminationTests(unittest.TestCase):
    """Regression tests: mode='done' must be a real terminal state for
    Calibrator.run() -- not just a boundary-decision label the loop ignores.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "state.json"
        self.history = root / "history.jsonl"
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def _seed_confirm_state(self, adapter):
        # One measurement away from done: known-good 540s, one bad sample
        # already at 600s (needs a second to confirm and finish, since
        # 600 - 540 <= MIN_BRACKET_SPAN leaves no room for a bracket).
        state = initial_state(adapter)
        state["sampling"]["startup_complete"] = True
        state["sampling"]["next_interval_seconds"] = 600
        state["sampling"]["last_measured_interval_seconds"] = 540
        state["controller"]["mode"] = MODE_CONFIRM
        state["controller"]["known_good_seconds"] = 540
        state["controller"]["confirmed_bad_seconds"] = None
        state["controller"]["candidate_seconds"] = 600
        state["controller"]["evidence"] = {"540": ["good", "good"], "600": ["bad"]}
        save_state(self.state, state)

    def _finish_to_done(self):
        adapter = FakeAdapter(self.clock, classifications=["bad"], update_baseline=[False])
        self._seed_confirm_state(adapter)
        rc = Calibrator(adapter, self.state, self.history, clock=self.clock,
                        sleeper=self.clock.sleep).run(max_measurements=5)
        return adapter, rc

    def test_transition_to_done_stops_immediately_after_terminal_measurement(self):
        adapter, rc = self._finish_to_done()
        self.assertEqual(rc, 0)
        self.assertEqual(adapter.probes, 1, "no probe after the terminal measurement")
        state = load_state(self.state)
        self.assertEqual(state["controller"]["mode"], MODE_DONE)
        self.assertEqual(state["controller"]["result"]["recommended_seconds"], 540)
        self.assertEqual(state["controller"]["result"]["bracket_low_seconds"], 540)
        self.assertEqual(state["controller"]["result"]["bracket_high_seconds"], 600)
        records = [r for r in load_history(self.history) if r["event"] == "measurement"]
        self.assertEqual(len(records), 1)

    def test_no_extra_wait_after_reaching_done(self):
        waits = []
        adapter = FakeAdapter(self.clock, classifications=["bad"], update_baseline=[False])
        self._seed_confirm_state(adapter)
        Calibrator(adapter, self.state, self.history, clock=self.clock,
                  sleeper=self.clock.sleep).run(max_measurements=5, on_wait=waits.append)
        # Exactly the one wait that precedes the terminal probe -- none after.
        self.assertEqual(len(waits), 1)

    def test_resume_of_already_done_state_makes_zero_probes(self):
        self._finish_to_done()
        before = load_state(self.state)
        self.assertEqual(before["controller"]["mode"], MODE_DONE)

        resumed_adapter = FakeAdapter(self.clock)
        rc = Calibrator(resumed_adapter, self.state, self.history, clock=self.clock,
                        sleeper=self.clock.sleep).run(max_measurements=5)
        self.assertEqual(rc, 0)
        self.assertEqual(resumed_adapter.probes, 0)
        after = load_state(self.state)
        self.assertEqual(after["controller"], before["controller"])
        self.assertEqual(after["sampling"], before["sampling"])

    def test_resume_of_done_state_emits_no_wait_and_no_new_measurement(self):
        self._finish_to_done()
        before_records = [r for r in load_history(self.history) if r["event"] == "measurement"]

        resumed_adapter = FakeAdapter(self.clock)
        waits = []
        rc = Calibrator(resumed_adapter, self.state, self.history, clock=self.clock,
                  sleeper=self.clock.sleep).run(max_measurements=5, on_wait=waits.append)
        self.assertEqual(rc, 0)
        self.assertEqual(waits, [])
        after_records = [r for r in load_history(self.history) if r["event"] == "measurement"]
        self.assertEqual(after_records, before_records)

    def test_max_measurements_still_stops_early_when_not_done(self):
        adapter = FakeAdapter(self.clock, classifications=["good"] * 5,
                              update_baseline=[False] * 5)
        rc = Calibrator(adapter, self.state, self.history, clock=self.clock,
                        sleeper=self.clock.sleep).run(max_measurements=2)
        self.assertEqual(rc, 0)
        state = load_state(self.state)
        self.assertNotEqual(state["controller"]["mode"], MODE_DONE)
        records = [r for r in load_history(self.history) if r["event"] == "measurement"]
        # startup + 2 interval measurements, run stops solely on the cap.
        self.assertEqual(len(records), 3)


class LegacyControllerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state_path = root / "state.json"
        self.history_path = root / "history.jsonl"
        self.clock = Clock()
        self.adapter = FakeAdapter(self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def write_v1(self, *, last_measured, nxt, progress, samples):
        state = initial_state(self.adapter)
        state["controller"] = {"schema": "agent-status-calibrator-controller-state/v1"}
        state["sampling"]["startup_complete"] = True
        state["sampling"]["last_measured_interval_seconds"] = last_measured
        state["sampling"]["next_interval_seconds"] = nxt
        state["sampling"]["progress"] = progress
        save_state(self.state_path, state)
        append_history(self.history_path, {"event": "run-started", "timestamp": "2026-09-18T05:00:00Z"})
        append_history(self.history_path, {
            "event": "measurement", "timestamp": "2026-09-18T05:00:01Z",
            "valid": True, "interval_seconds": None, "classification": "bad",
            "agent": "test-agent",
        })
        for index, (interval, label) in enumerate(samples):
            append_history(self.history_path, {
                "event": "measurement",
                "timestamp": f"2026-09-18T06:00:{index:02d}Z",
                "valid": True, "interval_seconds": interval, "classification": label,
                "agent": "test-agent",
            })
        append_history(self.history_path, {"event": "activity-detected", "timestamp": "2026-09-18T07:00:00Z"})

    def load(self):
        return Calibrator(self.adapter, self.state_path, self.history_path, clock=self.clock).load()[0]

    def test_legacy_900_bad_confirms_frontier_not_1200(self):
        self.write_v1(last_measured=900, nxt=1200, progress=8, samples=list(LIVE))
        state = self.load()
        self.assertEqual(state["controller"]["mode"], MODE_CONFIRM)
        self.assertEqual(state["controller"]["candidate_seconds"], 900)
        self.assertEqual(state["controller"]["evidence"]["900"], ["bad"])
        self.assertEqual(state["controller"]["evidence"]["600"], ["good"])
        self.assertNotIn("60", state["controller"]["evidence"])
        self.assertNotIn("180", state["controller"]["evidence"])
        self.assertNotIn("240", state["controller"]["evidence"])
        self.assertIsNone(state["controller"]["confirmed_bad_seconds"])
        self.assertIsNone(state["controller"]["known_good_seconds"])
        self.assertEqual(state["sampling"]["next_interval_seconds"], 900)
        self.assertEqual(state["sampling"]["progress"], 7)
        self.assertEqual(load_state(self.state_path)["controller"]["schema"],
                         state["controller"]["schema"])

    def test_progress_rollback_requires_mechanical_explorer_successor(self):
        self.write_v1(last_measured=900, nxt=999, progress=8, samples=list(LIVE))
        state = self.load()
        self.assertEqual(state["sampling"]["next_interval_seconds"], 900)
        self.assertEqual(state["sampling"]["progress"], 8)
        self.assertEqual(state["controller"]["mode"], MODE_CONFIRM)

    def test_progress_rollback_on_virgin_ladder_steps(self):
        # After first 1m: progress 1, next still 60 = _next_interval(1, 60).
        self.write_v1(last_measured=60, nxt=60, progress=1, samples=[(60, "bad")])
        state = self.load()
        self.assertEqual(state["sampling"]["progress"], 0)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 60)
        # After second 1m: progress 2, next 120 = _next_interval(2, 60).
        self.write_v1(last_measured=60, nxt=120, progress=2, samples=[(60, "good"), (60, "bad")])
        state = self.load()
        self.assertEqual(state["sampling"]["progress"], 1)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 60)
        # After 4m: progress 5, next 300 = VIRGIN[5] = _next_interval(5, 240).
        self.write_v1(last_measured=240, nxt=300, progress=5, samples=[(240, "bad")])
        state = self.load()
        self.assertEqual(state["sampling"]["progress"], 4)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 240)

    def test_old_bads_behind_frontier_do_not_capture_mode(self):
        self.write_v1(last_measured=900, nxt=1200, progress=8, samples=list(LIVE))
        state = self.load()
        self.assertEqual(state["controller"]["candidate_seconds"], 900)
        self.assertEqual(state["controller"]["evidence"].get("60"), None)

    def test_v2_state_is_not_remigrated(self):
        state = initial_state(self.adapter)
        state["controller"]["mode"] = MODE_BRACKET
        state["controller"]["candidate_seconds"] = 720
        state["sampling"]["last_measured_interval_seconds"] = 900
        state["sampling"]["next_interval_seconds"] = 720
        save_state(self.state_path, state)
        append_history(self.history_path, {
            "event": "measurement", "timestamp": "2026-09-18T06:00:00Z",
            "valid": True, "interval_seconds": 900, "classification": "bad",
        })
        loaded = self.load()
        self.assertEqual(loaded["controller"]["mode"], MODE_BRACKET)
        self.assertEqual(loaded["controller"]["candidate_seconds"], 720)
        self.assertEqual(loaded["sampling"]["next_interval_seconds"], 720)

    def test_legacy_good_frontier_stays_explore(self):
        self.write_v1(last_measured=600, nxt=900, progress=7, samples=LIVE[:-1])
        state = self.load()
        self.assertEqual(state["controller"]["mode"], MODE_EXPLORE)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 900)
        self.assertEqual(state["sampling"]["progress"], 7)
        self.assertEqual(state["controller"]["evidence"]["600"], ["good"])
        self.assertIsNone(state["controller"]["candidate_seconds"])
        self.assertIsNone(state["controller"]["known_good_seconds"])

    def test_legacy_ok_classifications_stay_compatible(self):
        self.write_v1(last_measured=60, nxt=120, progress=2, samples=[(60, "ok")])
        state = self.load()
        self.assertEqual(state["controller"]["mode"], MODE_EXPLORE)
        self.assertEqual(state["sampling"]["next_interval_seconds"], 120)
        self.assertEqual(state["sampling"]["progress"], 2)
        self.assertEqual(state["controller"]["evidence"], {})

    def test_startup_without_interval_is_not_evidence(self):
        self.write_v1(last_measured=60, nxt=60, progress=1, samples=[(60, "good")])
        state = self.load()
        self.assertNotIn("None", state["controller"]["evidence"])
        self.assertEqual(state["controller"]["evidence"]["60"], ["good"])

    def test_migration_is_deterministic_with_noise(self):
        self.write_v1(last_measured=900, nxt=1200, progress=8, samples=list(LIVE))
        first = self.load()["controller"]
        second = Calibrator(self.adapter, self.state_path, self.history_path, clock=self.clock).load()[0]["controller"]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
