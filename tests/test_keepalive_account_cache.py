"""Keepalive: Omarchy per-account cache files (claude-limits-<account-id>.json)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentstatus.keepalive.observe import observe_claude  # noqa: E402

KEYS = ("AGENT_STATUS_CLAUDE_JSON", "AGENT_STATUS_OMARCHY_CACHE")


def _payload(fetched_ms, week_pct):
    return json.dumps({
        "fetchedAtMs": fetched_ms,
        "limits": [
            {"label": "Session (5-hour)", "percent": 0.09, "resetsAt": "2099-01-01T00:00:00+00:00"},
            {"label": "Weekly (7-day)", "percent": week_pct, "resetsAt": "2099-01-01T00:00:00+00:00"},
        ],
    })


class KeepaliveAccountCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.saved = {k: os.environ.get(k) for k in KEYS}
        os.environ["AGENT_STATUS_CLAUDE_JSON"] = str(self.dir / "no-claude.json")
        os.environ["AGENT_STATUS_OMARCHY_CACHE"] = str(self.dir / "claude-limits.json")

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_newer_account_file_wins_over_stale_legacy_file(self):
        (self.dir / "claude-limits.json").write_text(_payload(1_000, 0.01))
        (self.dir / "claude-limits-48b711ed.json").write_text(_payload(2_000, 0.09))
        snapshot = observe_claude(self.dir / "claude")
        self.assertAlmostEqual(snapshot.weekly.used_percent, 9.0, places=3)

    def test_account_file_alone_is_read(self):
        (self.dir / "claude-limits-48b711ed.json").write_text(_payload(2_000, 0.5))
        snapshot = observe_claude(self.dir / "claude")
        self.assertAlmostEqual(snapshot.weekly.used_percent, 50.0, places=3)

    def test_conflicted_copy_is_ignored(self):
        (self.dir / "claude-limits [conflicted].json").write_text(_payload(9_000, 0.77))
        (self.dir / "claude-limits.json").write_text(_payload(1_000, 0.02))
        snapshot = observe_claude(self.dir / "claude")
        self.assertAlmostEqual(snapshot.weekly.used_percent, 2.0, places=3)


if __name__ == "__main__":
    unittest.main()
