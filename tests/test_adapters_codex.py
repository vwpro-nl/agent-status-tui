import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _fixtures as fx
from agentstatus.adapters import codex

NOW = 1_000_000_000.0


class CodexAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = fx.make_env(Path(self.tmp))
        self._orig_app_server = codex._app_server_snapshot
        self.addCleanup(setattr, codex, "_app_server_snapshot", self._orig_app_server)

    def test_detect_unused_when_no_sessions(self):
        d = codex.detect(self.env)
        self.assertFalse(d.ever_used)

    def test_detect_activity_is_the_newest_of_the_signals(self):
        fx.codex_rollout(self.env, primary_pct=34, secondary_pct=46, now=NOW,
                         event_age=120)          # rollout mtime -> NOW - 120
        fx.codex_history(self.env, ts=NOW - 300)  # older
        d = codex.detect(self.env)
        self.assertTrue(d.ever_used)
        self.assertAlmostEqual(d.last_activity, NOW - 120, delta=5)

    def test_detect_activity_picks_history_when_it_is_newer(self):
        fx.codex_rollout(self.env, primary_pct=1, secondary_pct=1, now=NOW,
                         event_age=5000)          # rollout mtime -> NOW - 5000
        fx.codex_history(self.env, ts=NOW - 90)   # newer
        d = codex.detect(self.env)
        self.assertAlmostEqual(d.last_activity, NOW - 90, delta=5)

    def test_live_app_server_yields_LIVE_and_no_age(self):
        codex._app_server_snapshot = lambda env: {
            "codex": {
                "primary": {"used_percent": 34.0, "resets_at": NOW + 1800,
                            "window_minutes": 300},
                "secondary": {"used_percent": 46.0, "resets_at": NOW + 5 * 86400,
                              "window_minutes": 10080},
            }
        }
        fx.codex_rollout(self.env, primary_pct=1, secondary_pct=1, now=NOW)  # present but unused
        status = codex.poll(self.env, NOW)
        self.assertEqual(status.freshness_kind, "live")
        self.assertIsNone(status.source_age)
        self.assertAlmostEqual(status.five_hour.used_percent, 34.0)
        self.assertAlmostEqual(status.weekly.used_percent, 46.0)
        self.assertEqual(status.availability, "ok")

    def test_falls_back_to_local_rollout_when_app_server_unavailable(self):
        codex._app_server_snapshot = lambda env: None
        fx.codex_rollout(self.env, primary_pct=34, secondary_pct=46, now=NOW, event_age=90)
        status = codex.poll(self.env, NOW)
        self.assertEqual(status.freshness_kind, "cache")
        self.assertAlmostEqual(status.source_age, 90, delta=2)
        self.assertAlmostEqual(status.five_hour.used_percent, 34.0)
        self.assertEqual(status.five_hour.nominal_minutes, 300)
        self.assertEqual(status.weekly.nominal_minutes, 10080)

    def test_no_data_at_all_is_no_capacity_data_not_a_crash(self):
        codex._app_server_snapshot = lambda env: None
        status = codex.poll(self.env, NOW)
        self.assertEqual(status.availability, "no-capacity-data")
        self.assertIsNone(status.five_hour)
        self.assertEqual(status.freshness_kind, "none")

    def test_primary_maps_to_5h_secondary_to_week(self):
        codex._app_server_snapshot = lambda env: None
        fx.codex_rollout(self.env, primary_pct=11, secondary_pct=77, now=NOW,
                         five_hour_left=1200, week_left=200000)
        status = codex.poll(self.env, NOW)
        self.assertAlmostEqual(status.five_hour.used_percent, 11.0)
        self.assertAlmostEqual(status.weekly.used_percent, 77.0)


if __name__ == "__main__":
    unittest.main()
