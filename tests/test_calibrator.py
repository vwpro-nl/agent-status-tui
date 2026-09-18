import datetime as dt
import tempfile
import unittest
from pathlib import Path

from agentstatus.calibrator.core import Calibrator, VIRGIN_INTERVALS
from agentstatus.calibrator.model import Activity, Assessment, Observation
from agentstatus.calibrator.persistence import load_history, load_state
from agentstatus.calibrator.render import chronological, interval_label, render_record


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

    def __init__(self, clock, validity=None):
        self.clock = clock
        self.validity = iter(validity or [])
        self.probes = 0
    def detect_activity(self):
        return Activity(self.clock.value - dt.timedelta(days=1), "fixture")
    def probe(self):
        self.probes += 1
        self.clock.value += dt.timedelta(seconds=7)
        valid = next(self.validity, True)
        return Observation(valid, f"m-{self.probes}", {"count": self.probes}, {"count": self.probes}, None if valid else "failed")
    def assess(self, observation, baseline):
        return Assessment("good" if observation.valid else "fail", {"seen": self.probes})


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

    def runner(self):
        return Calibrator(self.adapter, self.state, self.history, clock=self.clock,
                          sleeper=self.clock.sleep, check_interval=30)

    def measurements(self):
        return [r for r in load_history(self.history) if r["event"] == "measurement"]

    def test_virgin_startup_and_linear_sampling_sequence(self):
        self.assertEqual(self.runner().run(max_measurements=8), 0)
        records = self.measurements()
        self.assertIsNone(records[0]["interval_seconds"])
        self.assertEqual([r["interval_seconds"] for r in records[1:]],
                         [60, 60, 120, 180, 240, 300, 600, 900])
        self.assertEqual(interval_label(records[0]), "startup")

    def test_restart_between_two_one_minute_measurements(self):
        self.runner().run(max_measurements=1)
        first_state = load_state(self.state)
        self.assertEqual(first_state["progress"], 1)
        self.assertEqual(first_state["next_interval_seconds"], 60)
        self.runner().run(max_measurements=1)
        self.assertEqual([r["interval_seconds"] for r in self.measurements()], [None, 60, 60])

    def test_resume_later_does_not_restart_virgin_sequence(self):
        self.runner().run(max_measurements=6)
        self.runner().run(max_measurements=2)
        self.assertEqual([r["interval_seconds"] for r in self.measurements()[-2:]], [600, 900])
        self.assertEqual(sum(r["interval_seconds"] is None for r in self.measurements()), 1)

    def test_invalid_measurement_does_not_advance_progress(self):
        self.adapter = FakeAdapter(self.clock, [True, False])
        self.assertEqual(self.runner().run(max_measurements=1), 1)
        state = load_state(self.state)
        self.assertEqual(state["progress"], 0)
        self.assertEqual(state["next_interval_seconds"], 60)

    def test_next_is_computed_from_probe_end(self):
        shown = []
        self.runner().run(max_measurements=1, emit=shown.append)
        first = shown[0]
        ended = dt.datetime.fromisoformat(first["timestamp"].replace("Z", "+00:00"))
        next_at = dt.datetime.fromisoformat(first["next_scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual((next_at - ended).total_seconds(), 60)

    def test_movement_rendering(self):
        self.assertEqual(interval_label({"interval_seconds": 60, "movement_seconds": 0}), "1m·")
        self.assertEqual(interval_label({"interval_seconds": 120, "movement_seconds": 60}), "2m↑1m")
        self.assertEqual(interval_label({"interval_seconds": 3300, "movement_seconds": -300}), "55m↓5m")

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
        self.assertEqual(load_state(self.state)["progress"], 1)
        self.assertEqual(load_state(other_state)["progress"], 2)


if __name__ == "__main__":
    unittest.main()
