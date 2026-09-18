import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentstatus.calibrator.adapters.claude import (
    CCUSAGE_COMMAND, ClaudeCalibratorAdapter, parse_active_block,
)
from agentstatus.calibrator.model import Observation


def block(read=10, create=0, total=20, cost=0.01):
    return {"blocks": [{"isActive": True, "id": "b", "startTime": "2026-09-18T05:00:00Z",
        "endTime": "2026-09-18T10:00:00Z", "actualEndTime": "2026-09-18T06:00:00Z",
        "tokenCounts": {"inputTokens": 2, "outputTokens": 3,
            "cacheCreationInputTokens": create, "cacheReadInputTokens": read},
        "totalTokens": total, "costUSD": cost}]}


class ClaudeCalibratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.projects = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def test_unattended_ccusage_uses_npx_yes(self):
        self.assertEqual(CCUSAGE_COMMAND[:2], ("npx", "--yes"))

    def test_classification_good_bad_and_init(self):
        adapter = ClaudeCalibratorAdapter(self.projects)
        base = {"cache_create_tokens": 0, "total_tokens": 1000, "cost_usd": .01}
        good = Observation(True, "g", {"cache_create_tokens": 20, "total_tokens": 1000, "cost_usd": .01})
        bad = Observation(True, "b", {"cache_create_tokens": 500, "total_tokens": 1000, "cost_usd": .05})
        init = Observation(True, "i", {"initial_block": True, "cache_create_tokens": 500,
                                        "total_tokens": 1000, "cost_usd": .05})
        self.assertEqual(adapter.assess(good, base).classification, "good")
        self.assertEqual(adapter.assess(bad, base).classification, "bad")
        self.assertEqual(adapter.assess(init, base).classification, "init")

    def test_probe_failure_is_normalized_and_does_not_leak_output(self):
        secret = "SECRET-BEARER"
        def runner(command):
            return subprocess.CompletedProcess(command, 7, secret, secret)
        adapter = ClaudeCalibratorAdapter(self.projects, runner=runner)
        result = adapter.probe()
        self.assertFalse(result.valid)
        self.assertNotIn(secret, result.error)

    def test_probe_measures_delta_around_minimal_call(self):
        replies = iter((block(read=10, total=20, cost=.01), None,
                        block(read=18, total=30, cost=.013)))
        commands = []
        def runner(command):
            commands.append(command)
            payload = next(replies)
            return subprocess.CompletedProcess(command, 0, "OK" if payload is None else json.dumps(payload), "")
        adapter = ClaudeCalibratorAdapter(self.projects, runner=runner)
        result = adapter.probe()
        self.assertTrue(result.valid)
        self.assertEqual(result.values["cache_read_tokens"], 8)
        self.assertEqual(result.values["total_tokens"], 10)
        self.assertIn("--safe-mode", commands[1])
        self.assertEqual(commands[0][:2], ["npx", "--yes"])

    def test_malformed_ccusage_is_rejected(self):
        with self.assertRaises(Exception):
            parse_active_block("not-json")


if __name__ == "__main__":
    unittest.main()
