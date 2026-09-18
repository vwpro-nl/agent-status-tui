import datetime as dt
import io
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from agentstatus.calibrator import cli
from agentstatus.calibrator.adapters.codex import (
    CodexCalibratorAdapter, CodexProbeError, parse_exec_usage, run_command,
)
from agentstatus.calibrator.model import STATUS_ACTIVITY, STATUS_NONE, STATUS_UNRELIABLE, Observation
from agentstatus.calibrator.render import render_record


UTC = dt.timezone.utc


def usage_event(**overrides):
    usage = {"input_tokens": 30, "cached_input_tokens": 20,
             "cache_write_input_tokens": 4, "output_tokens": 6, "total_tokens": 36}
    usage.update(overrides)
    return json.dumps({"type": "turn.completed", "usage": usage}) + "\n"


class CodexActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "codex"
        self.home.mkdir()
        self.adapter = CodexCalibratorAdapter(self.home, model="fixture-model")
    def tearDown(self):
        self.temp.cleanup()

    def test_activity_from_rollout_mtime(self):
        path = self.home / "sessions/2026/09/18/rollout-a.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
        os.utime(path, (1_789_710_000, 1_789_710_000))
        result = self.adapter.detect_activity()
        self.assertEqual((result.status, result.source), (STATUS_ACTIVITY, "codex-rollout"))
        self.assertEqual(result.at, dt.datetime.fromtimestamp(1_789_710_000, UTC))

    def test_activity_from_history(self):
        (self.home / "history.jsonl").write_text(json.dumps({"ts": 1_789_710_100}) + "\n")
        result = self.adapter.detect_activity()
        self.assertEqual((result.status, result.source), (STATUS_ACTIVITY, "codex-history"))
        self.assertIsNotNone(result.at.tzinfo)

    def test_activity_from_sqlite(self):
        with sqlite3.connect(self.home / "state_5.sqlite") as database:
            database.execute("CREATE TABLE threads(updated_at REAL, updated_at_ms INTEGER)")
            database.execute("INSERT INTO threads VALUES (?, ?)", (1_789_710_200, None))
        result = self.adapter.detect_activity()
        self.assertEqual((result.status, result.source), (STATUS_ACTIVITY, "codex-sqlite"))

    def test_one_failed_source_does_not_invalidate_working_source(self):
        with patch.object(self.adapter, "_rollout_activity", side_effect=OSError("fixture")), \
             patch.object(self.adapter, "_history_activity", return_value=dt.datetime(2026, 9, 18, tzinfo=UTC)):
            result = self.adapter.detect_activity()
        self.assertEqual(result.status, STATUS_ACTIVITY)
        self.assertEqual(result.source, "codex-history")
        self.assertEqual(result.reliable_checks, 2)
        self.assertEqual(len(result.failed_checks), 1)

    def test_all_checks_unavailable_is_unreliable(self):
        with patch.object(self.adapter, "_rollout_activity", side_effect=OSError()), \
             patch.object(self.adapter, "_history_activity", side_effect=OSError()), \
             patch.object(self.adapter, "_sqlite_activity", side_effect=sqlite3.Error()):
            result = self.adapter.detect_activity()
        self.assertEqual(result.status, STATUS_UNRELIABLE)
        self.assertEqual(result.reliable_checks, 0)

    def test_checked_empty_environment_is_none(self):
        result = self.adapter.detect_activity()
        self.assertEqual(result.status, STATUS_NONE)
        self.assertEqual(result.reliable_checks, 3)
        self.assertIsNone(result.at)


class CodexProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.home.mkdir()
    def tearDown(self):
        self.temp.cleanup()

    def test_safe_exec_argv_and_direct_stdout_measurement(self):
        calls = []
        environments = []
        def runner(command, *, env):
            calls.append(command)
            environments.append(env)
            return subprocess.CompletedProcess(command, 0, usage_event(), "")
        adapter = CodexCalibratorAdapter(self.home, model="codex-fixture", prompt="Reply only: OK",
                                         codex_command=["/bin/codex"], scratch_root=self.root, runner=runner)
        with patch.dict(os.environ, {"CALIBRATOR_INHERITED_FIXTURE": "preserved"}):
            result = adapter.probe()
        self.assertTrue(result.valid)
        command = calls[0]
        self.assertEqual(command[:8], ["/bin/codex", "exec", "--sandbox", "read-only",
                                      "--skip-git-repo-check", "--json", "--color", "never"])
        self.assertEqual(command[8:11], ["-m", "codex-fixture", "-C"])
        self.assertEqual(command[-1], "Reply only: OK")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(environments[0]["CODEX_HOME"], str(self.home))
        self.assertEqual(environments[0]["CALIBRATOR_INHERITED_FIXTURE"], "preserved")
        self.assertEqual(result.values["cached_input_tokens"], 20)
        self.assertEqual(result.values["evidence"], "stdout")
        self.assertNotIn("cost_usd", result.values)
        self.assertEqual(result.display, {"c_read": 20, "c_write": 4, "input": 30,
                                          "output": 6, "total": 36, "cost": "-"})
        # Cached input is attribution within provider-reported input and is
        # never added to TOTAL a second time.
        self.assertEqual(result.display["total"], 36)
        self.assertNotEqual(result.display["total"], 20 + 4 + 30 + 6)
        line = render_record({"timestamp": "2026-09-18T06:00:00Z",
                              "interval_seconds": 60, "next_movement_seconds": 0,
                              "display": result.display}, "CODEX")
        self.assertIn("20        4         30        6         36        -", line)

    def test_parse_nested_token_usage(self):
        raw = json.dumps({"type": "event", "payload": {"info": {"last_token_usage": {
            "input_tokens": 8, "cached_input_tokens": 3, "cache_write_input_tokens": 1,
            "output_tokens": 2, "total_tokens": 10}}}})
        self.assertEqual(parse_exec_usage(raw)["cache_write_input_tokens"], 1)

    def test_invalid_reported_total_is_not_replaced_with_a_fabricated_value(self):
        self.assertIsNone(parse_exec_usage(usage_event(total_tokens="invalid")))

    def test_missing_token_usage_is_invalid(self):
        runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, '{"type":"message"}\n', "")
        result = CodexCalibratorAdapter(self.home, model="fixture", runner=runner).probe()
        self.assertFalse(result.valid)
        self.assertIn("no usable token usage", result.error)

    def test_rollout_fallback_requires_one_probe_changed_file(self):
        session = self.home / "sessions/2026/09/18/rollout-probe.jsonl"
        session.parent.mkdir(parents=True)
        now = 1_789_710_000.0
        def runner(command, **kwargs):
            event = {"timestamp": dt.datetime.fromtimestamp(now, UTC).isoformat(),
                     "payload": {"type": "token_count", "cwd": command[-2], "info": {"last_token_usage": {
                         "input_tokens": 9, "cached_input_tokens": 2, "cache_write_input_tokens": 1,
                         "output_tokens": 3, "total_tokens": 12}}}}
            session.write_text(json.dumps(event) + "\n")
            return subprocess.CompletedProcess(command, 0, "{}\n", "")
        adapter = CodexCalibratorAdapter(self.home, model="fixture", runner=runner, clock=lambda: now)
        result = adapter.probe()
        self.assertTrue(result.valid)
        self.assertEqual(result.values["evidence"], "probe-rollout")

    def test_assessment_is_only_ok_or_fail_and_never_updates_baseline(self):
        adapter = CodexCalibratorAdapter(self.home, model="fixture")
        valid = Observation(True, "v", {"total_tokens": 1})
        invalid = Observation(False, "x", error="timeout")
        self.assertEqual(adapter.assess(valid, {}).classification, "ok")
        failed = adapter.assess(invalid, {"kept": True})
        self.assertEqual(failed.classification, "fail")
        self.assertFalse(failed.update_baseline)
        self.assertEqual(failed.baseline, {"kept": True})

    def test_timeout_kills_process_group(self):
        class Process:
            pid = 4242
            calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("codex", timeout)
                return "", ""
        with patch("agentstatus.calibrator.adapters.codex.subprocess.Popen", return_value=Process()), \
             patch("agentstatus.calibrator.adapters.codex.os.killpg") as killpg:
            with self.assertRaises(CodexProbeError):
                run_command(["codex", "exec"], timeout=1)
        killpg.assert_called_once_with(4242, signal.SIGKILL)


class CodexCliTests(unittest.TestCase):
    def test_cli_selects_codex_adapter_without_running_probe_for_status(self):
        fake = Mock(display_name="CODEX", display_columns=())
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "CodexCalibratorAdapter", return_value=fake) as constructor, \
             redirect_stdout(io.StringIO()):
            result = cli.main(["status", "--agent", "codex", "--model", "fixture",
                               "--state-dir", directory])
        self.assertEqual(result, 0)
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.kwargs["model"], "fixture")


if __name__ == "__main__":
    unittest.main()
