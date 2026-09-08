import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _fixtures as fx
from agentstatus import poll as poll_mod
from agentstatus.adapters import codex
from agentstatus.model import AgentStatus, Window

NOW = 1_000_000_000.0


class DetectionAndOrderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = fx.make_env(Path(self.tmp))
        self._orig = codex._app_server_snapshot
        codex._app_server_snapshot = lambda env: None
        self.addCleanup(setattr, codex, "_app_server_snapshot", self._orig)

    def test_never_used_providers_are_hidden(self):
        # only grok has any local evidence
        fx.grok_session(self.env, last_active_at=NOW - 30)
        rows = poll_mod.collect(self.env, NOW)
        self.assertEqual([r.key for r in rows], ["grok"])

    def test_rows_sorted_by_recent_activity_newest_first(self):
        fx.codex_rollout(self.env, primary_pct=5, secondary_pct=5, now=NOW)
        fx.codex_history(self.env, ts=NOW - 10_000)
        os = __import__("os")
        # force codex rollout mtime old so codex activity < claude < grok
        rp = next((self.env.codex_home / "sessions").rglob("rollout-*.jsonl"))
        os.utime(rp, (NOW - 10_000, NOW - 10_000))
        fx.claude_omarchy(self.env, fetched_at=NOW - 50, now=NOW)
        fx.claude_activity(self.env, transcript_ts=NOW - 5_000)
        fx.grok_session(self.env, last_active_at=NOW - 100)
        rows = poll_mod.collect(self.env, NOW)
        self.assertEqual([r.key for r in rows], ["grok", "claude", "codex"])

    def test_ordering_is_not_by_token_or_percent(self):
        # grok (no capacity) most recent -> still first, ahead of a busy codex
        fx.codex_rollout(self.env, primary_pct=99, secondary_pct=99, now=NOW)
        rp = next((self.env.codex_home / "sessions").rglob("rollout-*.jsonl"))
        __import__("os").utime(rp, (NOW - 4000, NOW - 4000))
        fx.grok_session(self.env, last_active_at=NOW - 10)
        rows = poll_mod.collect(self.env, NOW)
        self.assertEqual(rows[0].key, "grok")

    def test_stale_provider_with_no_window_is_hidden(self):
        old = NOW - 40 * 24 * 3600
        fx.grok_session(self.env, last_active_at=old)
        rows = poll_mod.collect(self.env, NOW)
        self.assertEqual(rows, [])

    def test_stale_provider_that_still_has_a_window_is_kept(self):
        old = NOW - 40 * 24 * 3600
        fx.claude_omarchy(self.env, fetched_at=NOW - 60, now=NOW)
        fx.claude_activity(self.env, transcript_ts=old)
        rows = poll_mod.collect(self.env, NOW)
        self.assertEqual([r.key for r in rows], ["claude"])


class FailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = fx.make_env(Path(self.tmp))

    def test_one_adapter_raising_does_not_stop_the_others(self):
        fx.claude_omarchy(self.env, fetched_at=NOW - 60, now=NOW)
        fx.claude_activity(self.env, transcript_ts=NOW - 100)
        fx.grok_session(self.env, last_active_at=NOW - 10)

        orig = codex.poll
        codex.poll = lambda env, now: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            # codex has evidence too, so it will be polled and will raise
            fx.codex_rollout(self.env, primary_pct=1, secondary_pct=1, now=NOW)
            rows = poll_mod.collect(self.env, NOW)
        finally:
            codex.poll = orig

        keys = {r.key for r in rows}
        self.assertIn("claude", keys)
        self.assertIn("grok", keys)
        codex_row = next(r for r in rows if r.key == "codex")
        self.assertEqual(codex_row.availability, "error")

    def test_failed_adapter_reuses_previous_good_row(self):
        fx.codex_rollout(self.env, primary_pct=1, secondary_pct=1, now=NOW)
        good = AgentStatus("codex", "CODEX", Window(20.0, NOW + 1000, 300), None,
                           "cache", 30.0, NOW - 100, "ok")
        orig = codex.poll
        codex.poll = lambda env, now: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            rows = poll_mod.collect(self.env, NOW, {"codex": good})
        finally:
            codex.poll = orig
        codex_row = next(r for r in rows if r.key == "codex")
        self.assertEqual(codex_row.availability, "error")
        self.assertIsNotNone(codex_row.five_hour)   # kept from the previous good row


if __name__ == "__main__":
    unittest.main()
