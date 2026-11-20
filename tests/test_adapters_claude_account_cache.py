"""Omarchy per-account cache files (claude-limits-<account-id>.json)."""

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentstatus.adapters import claude  # noqa: E402
from agentstatus.env import Env  # noqa: E402

NOW = 1_795_172_700.0


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def _payload(fetched_at, week_pct):
    return json.dumps({
        "fetchedAtMs": int(fetched_at * 1000),
        "limits": [
            {"label": "Session (5-hour)", "percent": 0.09, "resetsAt": _iso(NOW + 3600)},
            {"label": "Weekly (7-day)", "percent": week_pct, "resetsAt": _iso(NOW + 86400)},
        ],
    })


class AccountCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cache_dir = root / "omarchy"
        self.cache_dir.mkdir()
        self.env = Env(
            codex_home=root / "codex",
            claude_home=root / "claude",
            claude_json=root / "claude.json",
            claude_projects=root / "claude" / "projects",
            omarchy_claude_cache=self.cache_dir / "claude-limits.json",
            grok_home=root / "grok",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_newer_account_file_wins_over_stale_legacy_file(self):
        (self.cache_dir / "claude-limits.json").write_text(_payload(NOW - 2 * 86400, 0.01))
        (self.cache_dir / "claude-limits-48b711ed.json").write_text(_payload(NOW - 60, 0.09))
        status = claude.poll(self.env, NOW)
        self.assertAlmostEqual(status.weekly.used_percent, 9.0, places=3)
        self.assertIsNotNone(status.five_hour)
        self.assertAlmostEqual(status.source_age, 60, delta=2)

    def test_account_file_alone_is_read(self):
        (self.cache_dir / "claude-limits-48b711ed.json").write_text(_payload(NOW - 30, 0.5))
        status = claude.poll(self.env, NOW)
        self.assertAlmostEqual(status.weekly.used_percent, 50.0, places=3)

    def test_conflicted_copy_is_ignored(self):
        (self.cache_dir / "claude-limits [conflicted].json").write_text(_payload(NOW - 5, 0.77))
        (self.cache_dir / "claude-limits.json").write_text(_payload(NOW - 60, 0.02))
        status = claude.poll(self.env, NOW)
        self.assertAlmostEqual(status.weekly.used_percent, 2.0, places=3)


if __name__ == "__main__":
    unittest.main()
