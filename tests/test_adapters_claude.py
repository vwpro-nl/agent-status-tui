import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _fixtures as fx
from agentstatus.adapters import claude

NOW = 1_000_000_000.0


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = fx.make_env(Path(self.tmp))

    def test_omarchy_preferred_over_claude_json(self):
        fx.claude_omarchy(self.env, fetched_at=NOW - 300, five_pct=0.63, week_pct=0.62, now=NOW)
        fx.claude_json_cache(self.env, fetched_at=NOW - 10, five_pct=27, week_pct=49, now=NOW)
        status = claude.poll(self.env, NOW)
        self.assertAlmostEqual(status.five_hour.used_percent, 63.0)   # 0.63 -> 63
        self.assertAlmostEqual(status.weekly.used_percent, 62.0)
        self.assertAlmostEqual(status.source_age, 300, delta=2)       # omarchy fetchedAt

    def test_falls_back_to_claude_json(self):
        fx.claude_json_cache(self.env, fetched_at=NOW - 500, five_pct=27, week_pct=49, now=NOW)
        status = claude.poll(self.env, NOW)
        self.assertAlmostEqual(status.five_hour.used_percent, 27.0)
        self.assertAlmostEqual(status.source_age, 500, delta=2)

    def test_claude_is_never_live(self):
        fx.claude_omarchy(self.env, fetched_at=NOW - 5, now=NOW)
        status = claude.poll(self.env, NOW)
        self.assertEqual(status.freshness_kind, "cache")
        self.assertNotEqual(status.freshness_kind, "live")
        self.assertIsNotNone(status.source_age)

    def test_ready_semantic_preserved(self):
        # exactly 0% and an empty reset string -> "ready" downstream
        fx.claude_omarchy(self.env, fetched_at=NOW - 60, five_pct=0.0, now=NOW, five_reset="")
        status = claude.poll(self.env, NOW)
        self.assertEqual(status.five_hour.used_percent, 0.0)
        self.assertIsNone(status.five_hour.resets_at)
        from agentstatus.model import time_budget

        self.assertEqual(time_budget(status.five_hour, NOW)[0], "ready")

    def test_expired_window_is_dropped(self):
        fx.claude_omarchy(self.env, fetched_at=NOW - 60, now=NOW, five_left=-3600,
                          week_left=52 * 3600)
        status = claude.poll(self.env, NOW)
        self.assertIsNone(status.five_hour)      # 5h reset already passed -> dropped
        self.assertIsNotNone(status.weekly)

    def test_no_capacity_source_is_not_a_crash(self):
        status = claude.poll(self.env, NOW)
        self.assertEqual(status.availability, "no-capacity-data")
        self.assertEqual(status.freshness_kind, "none")

    def test_activity_uses_transcripts_not_cache_timestamp(self):
        # cache is fresh (5s) but the user last acted 2h ago -> activity must
        # reflect the user, not fetchedAtMs
        fx.claude_omarchy(self.env, fetched_at=NOW - 5, now=NOW)
        fx.claude_activity(self.env, transcript_ts=NOW - 7200, history_ts=NOW - 9000)
        d = claude.detect(self.env)
        self.assertAlmostEqual(d.last_activity, NOW - 7200, delta=5)


if __name__ == "__main__":
    unittest.main()
