import datetime as dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentstatus.calibrator.adapters.claude import (
    CCUSAGE_COMMAND, ClaudeCalibratorAdapter, ClaudeProbeError, parse_active_block, run_command,
)
from agentstatus.calibrator.model import STATUS_ACTIVITY, STATUS_NONE, STATUS_UNRELIABLE, Observation
from agentstatus.calibrator.persistence import append_history
from agentstatus.calibrator.render import render_record


def block(read=10, create=0, total=20, cost=0.01, actual_end="2026-09-18T06:00:00Z"):
    return {"blocks": [{"isActive": True, "id": "b", "startTime": "2026-09-18T05:00:00Z",
        "endTime": "2026-09-18T10:00:00Z", "actualEndTime": actual_end,
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
        self.assertEqual(result.display, {"c_read": 8, "c_write": 0, "input": 0,
                                          "output": 0, "total": 10, "cost": "$0.0030"})
        line = render_record({"timestamp": "2026-09-18T06:00:00Z",
                              "interval_seconds": 60, "next_movement_seconds": 0,
                              "display": result.display}, "CLAUDE")
        self.assertIn("8         0         0         0         10        $0.0030", line)
        self.assertIn("--safe-mode", commands[1])
        self.assertEqual(commands[0][:2], ["npx", "--yes"])

    def test_malformed_ccusage_is_rejected(self):
        with self.assertRaises(Exception):
            parse_active_block("not-json")

    # -- baseline contract --

    def test_good_updates_claude_baseline(self):
        adapter = ClaudeCalibratorAdapter(self.projects)
        base = {"cache_create_tokens": 0, "total_tokens": 1000, "cost_usd": .01}
        good = Observation(True, "g", {"cache_create_tokens": 20, "total_tokens": 1000, "cost_usd": .01})
        assessment = adapter.assess(good, base)
        self.assertEqual(assessment.classification, "good")
        self.assertTrue(assessment.update_baseline)
        self.assertNotEqual(assessment.baseline, base)

    def test_bad_does_not_update_claude_baseline(self):
        adapter = ClaudeCalibratorAdapter(self.projects)
        base = {"cache_create_tokens": 0, "total_tokens": 1000, "cost_usd": .01}
        bad = Observation(True, "b", {"cache_create_tokens": 500, "total_tokens": 1000, "cost_usd": .05})
        assessment = adapter.assess(bad, base)
        self.assertEqual(assessment.classification, "bad")
        self.assertFalse(assessment.update_baseline)
        self.assertEqual(assessment.baseline, base)

    def test_init_does_not_update_claude_baseline(self):
        adapter = ClaudeCalibratorAdapter(self.projects)
        base = {"cache_create_tokens": 0, "total_tokens": 1000, "cost_usd": .01}
        init = Observation(True, "i", {"initial_block": True, "cache_create_tokens": 500,
                                        "total_tokens": 1000, "cost_usd": .05})
        assessment = adapter.assess(init, base)
        self.assertEqual(assessment.classification, "init")
        self.assertFalse(assessment.update_baseline)
        self.assertEqual(assessment.baseline, base)

    def test_fail_does_not_update_claude_baseline(self):
        adapter = ClaudeCalibratorAdapter(self.projects)
        base = {"cache_create_tokens": 0, "total_tokens": 1000, "cost_usd": .01}
        invalid = Observation(False, "x", error="boom")
        assessment = adapter.assess(invalid, base)
        self.assertEqual(assessment.classification, "fail")
        self.assertFalse(assessment.update_baseline)
        self.assertEqual(assessment.baseline, base)

    def test_startup_baseline_only_when_explicitly_allowed(self):
        good = Observation(True, "g", {"cache_create_tokens": 5, "total_tokens": 100, "cost_usd": .001})
        cautious = ClaudeCalibratorAdapter(self.projects, startup_baseline=False)
        assessment = cautious.assess(good, {}, startup=True)
        self.assertEqual(assessment.classification, "good")
        self.assertFalse(assessment.update_baseline)

        opted_in = ClaudeCalibratorAdapter(self.projects, startup_baseline=True)
        assessment = opted_in.assess(good, {}, startup=True)
        self.assertEqual(assessment.classification, "good")
        self.assertTrue(assessment.update_baseline)

    def test_startup_baseline_flag_never_overrides_bad_or_init(self):
        adapter = ClaudeCalibratorAdapter(self.projects, startup_baseline=True)
        bad = Observation(True, "b", {"cache_create_tokens": 5000, "total_tokens": 100, "cost_usd": 5})
        init = Observation(True, "i", {"initial_block": True, "cache_create_tokens": 5, "total_tokens": 100})
        self.assertFalse(adapter.assess(bad, {}, startup=True).update_baseline)
        self.assertFalse(adapter.assess(init, {}, startup=True).update_baseline)

    def test_good_still_updates_baseline_outside_startup_regardless_of_flag(self):
        good = Observation(True, "g", {"cache_create_tokens": 5, "total_tokens": 100, "cost_usd": .001})
        adapter = ClaudeCalibratorAdapter(self.projects, startup_baseline=False)
        self.assertTrue(adapter.assess(good, {}, startup=False).update_baseline)

    # -- activity model --

    def _write_transcript(self, timestamp: str) -> None:
        session = self.projects / "proj" / "session.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        entry = {"type": "assistant", "timestamp": timestamp,
                 "message": {"usage": {"input_tokens": 5, "output_tokens": 1}}}
        session.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    def test_activity_from_both_sources_picks_the_newest(self):
        self._write_transcript("2026-09-18T05:00:00Z")
        def runner(command):
            return subprocess.CompletedProcess(command, 0, json.dumps(block(actual_end="2026-09-18T06:00:00Z")), "")
        adapter = ClaudeCalibratorAdapter(self.projects, runner=runner)
        result = adapter.detect_activity()
        self.assertEqual(result.status, STATUS_ACTIVITY)
        self.assertEqual(result.source, "ccusage-actualEndTime")
        self.assertEqual(result.reliable_checks, 2)
        self.assertEqual(result.at, dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.timezone.utc))

    def test_one_activity_source_fails_but_the_other_is_reliable(self):
        # No transcript directory at all -> the transcript check fails, but
        # ccusage is independently reliable and finds activity.
        missing = self.projects / "does-not-exist"
        def runner(command):
            return subprocess.CompletedProcess(command, 0, json.dumps(block(actual_end="2026-09-18T06:00:00Z")), "")
        adapter = ClaudeCalibratorAdapter(missing, runner=runner)
        result = adapter.detect_activity()
        self.assertEqual(result.status, STATUS_ACTIVITY)
        self.assertEqual(result.reliable_checks, 1)
        self.assertEqual(len(result.failed_checks), 1)
        self.assertIn("claude-transcript", result.failed_checks[0])

    def test_all_reliable_sources_find_no_activity_is_none(self):
        def runner(command):
            return subprocess.CompletedProcess(command, 0, json.dumps({"blocks": []}), "")
        adapter = ClaudeCalibratorAdapter(self.projects, runner=runner)
        result = adapter.detect_activity()
        self.assertEqual(result.status, STATUS_NONE)
        self.assertIsNone(result.at)
        self.assertEqual(result.reliable_checks, 2)

    def test_all_relevant_activity_sources_insufficiently_reliable(self):
        missing = self.projects / "does-not-exist"
        def runner(command):
            return subprocess.CompletedProcess(command, 1, "", "boom")
        adapter = ClaudeCalibratorAdapter(missing, runner=runner)
        result = adapter.detect_activity()
        self.assertEqual(result.status, STATUS_UNRELIABLE)
        self.assertIsNone(result.at)
        self.assertEqual(result.reliable_checks, 0)
        self.assertEqual(len(result.failed_checks), 2)

    def test_probe_log_fallback_never_counts_as_a_reliable_check(self):
        # Both primary sources fail, and only a probe-log candidate exists:
        # this must still be unreliable, never "activity" from probe-log
        # alone, and never "none".
        missing = self.projects / "does-not-exist"
        history_path = Path(self.temp.name) / "history.jsonl"
        append_history(history_path, {
            "event": "measurement", "timestamp": "2026-09-18T05:30:00Z", "valid": True,
            "agent": "claude", "provider": "anthropic", "measurement_schema": "claude-ccusage/v1",
        })
        def runner(command):
            return subprocess.CompletedProcess(command, 1, "", "boom")
        adapter = ClaudeCalibratorAdapter(missing, runner=runner, history_path=history_path)
        result = adapter.detect_activity()
        self.assertEqual(result.status, STATUS_UNRELIABLE)
        self.assertEqual(result.reliable_checks, 0)

    def test_probe_log_fallback_supplements_candidates_when_reliable(self):
        history_path = Path(self.temp.name) / "history.jsonl"
        append_history(history_path, {
            "event": "measurement", "timestamp": "2026-09-18T07:00:00Z", "valid": True,
            "agent": "claude", "provider": "anthropic", "measurement_schema": "claude-ccusage/v1",
        })
        def runner(command):
            return subprocess.CompletedProcess(command, 0, json.dumps({"blocks": []}), "")
        adapter = ClaudeCalibratorAdapter(self.projects, runner=runner, history_path=history_path)
        result = adapter.detect_activity()
        # Transcript + ccusage are both reliably checked (2), even though
        # neither found anything; the probe-log candidate still wins as the
        # only actual evidence of activity.
        self.assertEqual(result.status, STATUS_ACTIVITY)
        self.assertEqual(result.source, "probe-log")
        self.assertEqual(result.reliable_checks, 2)

    # -- subprocess timeout / process-group cleanup --

    def test_subprocess_timeout_raises_claude_probe_error(self):
        class FakeProcess:
            pid = 4242
            def __init__(self):
                self.calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
                return ("", "")
        with patch("agentstatus.calibrator.adapters.claude.subprocess.Popen", return_value=FakeProcess()), \
             patch("agentstatus.calibrator.adapters.claude.os.killpg") as killpg:
            with self.assertRaises(ClaudeProbeError):
                run_command(["claude"], timeout=1.0)
            killpg.assert_called_once_with(4242, __import__("signal").SIGKILL)

    def test_subprocess_timeout_falls_back_to_process_kill_if_group_kill_fails(self):
        class FakeProcess:
            pid = 99
            def __init__(self):
                self.killed = False
            def communicate(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
                return ("", "")
            def kill(self):
                self.killed = True
        process = FakeProcess()
        with patch("agentstatus.calibrator.adapters.claude.subprocess.Popen", return_value=process), \
             patch("agentstatus.calibrator.adapters.claude.os.killpg", side_effect=ProcessLookupError()):
            with self.assertRaises(ClaudeProbeError):
                run_command(["claude"], timeout=1.0)
            self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
