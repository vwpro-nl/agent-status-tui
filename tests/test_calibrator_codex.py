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
    CodexCalibratorAdapter, CodexProbeError, five_hour_budget_fields, parse_exec_usage,
    parse_rate_limit_windows, run_command, weekly_budget_fields,
)
from agentstatus.calibrator.model import STATUS_ACTIVITY, STATUS_NONE, STATUS_UNRELIABLE, Observation
from agentstatus.calibrator.render import render_record


UTC = dt.timezone.utc


def limits(primary_used=18, primary_reset=100, weekly_used=87, weekly_reset=200):
    snapshot = {}
    if primary_used is not None:
        snapshot["primary"] = {"used_percent": primary_used, "resets_at": primary_reset}
    else:
        snapshot["primary"] = None
    if weekly_used is not None:
        snapshot["secondary"] = {"used_percent": weekly_used, "resets_at": weekly_reset}
    else:
        snapshot["secondary"] = None
    return snapshot


def steady_window(used=18, reset=1_789_742_498, weekly_used=87, weekly_reset=1_789_805_362):
    snapshot = limits(used, reset, weekly_used, weekly_reset)
    return lambda: snapshot


def scripted_windows(*windows):
    remaining = iter(windows)
    return lambda: next(remaining)


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
        adapter = CodexCalibratorAdapter(
            self.home, model="codex-fixture", prompt="Reply only: OK",
            codex_command=["/bin/codex"], scratch_root=self.root, runner=runner,
            rate_limit_reader=steady_window(),
        )
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
        self.assertEqual(result.values["five_hour_used_percent_before"], 18)
        self.assertEqual(result.values["five_hour_used_percent_after"], 18)
        self.assertEqual(result.values["five_hour_resets_at"], 1_789_742_498)
        self.assertEqual(result.values["five_hour_delta"], 0)
        self.assertTrue(result.values["five_hour_isolated"])
        self.assertIs(result.values["five_hour_window_reset"], False)
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
        result = CodexCalibratorAdapter(
            self.home, model="fixture", runner=runner, rate_limit_reader=steady_window(),
        ).probe()
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
        adapter = CodexCalibratorAdapter(
            self.home, model="fixture", runner=runner, clock=lambda: now,
            rate_limit_reader=steady_window(),
        )
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


class CodexFiveHourInstrumentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.home.mkdir()
    def tearDown(self):
        self.temp.cleanup()

    def probe_with(self, reader, runner=None, **kwargs):
        def default_runner(command, **kw):
            return subprocess.CompletedProcess(command, 0, usage_event(), "")
        adapter = CodexCalibratorAdapter(
            self.home, model="fixture", scratch_root=self.root,
            runner=runner or default_runner, rate_limit_reader=reader, **kwargs,
        )
        return adapter.probe()

    def test_one_passive_read_returns_both_windows(self):
        payload = {"result": {"rateLimits": {
            "primary": {"usedPercent": 18, "windowDurationMins": 300, "resetsAt": 1789742498},
            "secondary": {"usedPercent": 87, "windowDurationMins": 10080, "resetsAt": 1789805362},
        }}}
        parsed = parse_rate_limit_windows(payload)
        self.assertEqual(parsed["primary"]["used_percent"], 18)
        self.assertIsInstance(parsed["primary"]["used_percent"], int)
        self.assertEqual(parsed["secondary"]["used_percent"], 87)
        self.assertIsInstance(parsed["secondary"]["used_percent"], int)
        calls = []
        def reader():
            calls.append(1)
            return parsed
        result = self.probe_with(reader)
        self.assertTrue(result.valid)
        self.assertEqual(len(calls), 2)

    def test_same_window_sample_is_isolated(self):
        result = self.probe_with(steady_window(18, 100))
        self.assertTrue(result.valid)
        self.assertEqual(result.values["five_hour_used_percent_before"], 18)
        self.assertEqual(result.values["five_hour_used_percent_after"], 18)
        self.assertEqual(result.values["five_hour_resets_at_before"], 100)
        self.assertEqual(result.values["five_hour_resets_at_after"], 100)
        self.assertEqual(result.values["five_hour_resets_at"], 100)
        self.assertEqual(result.values["five_hour_delta"], 0)
        self.assertTrue(result.values["five_hour_isolated"])
        self.assertIs(result.values["five_hour_window_reset"], False)
        self.assertEqual(result.display["cost"], "-")
        self.assertNotIn("cost_usd", result.values)

    def test_zero_delta_is_not_treated_as_zero_cost(self):
        fields = five_hour_budget_fields(
            {"used_percent": 18, "resets_at": 50},
            {"used_percent": 18, "resets_at": 50},
            True,
        )
        self.assertEqual(fields["five_hour_delta"], 0)
        self.assertTrue(fields["five_hour_isolated"])
        # Integer delta 0 only means no 1pp crossing was observed.

    def test_one_percent_crossing(self):
        result = self.probe_with(scripted_windows(
            limits(18, 50, 87, 200),
            limits(19, 50, 87, 200),
        ))
        self.assertEqual(result.values["five_hour_delta"], 1)
        self.assertTrue(result.values["five_hour_isolated"])
        self.assertEqual(result.display["cost"], "-")

    def test_window_reset_invalidates_delta(self):
        result = self.probe_with(scripted_windows(
            limits(99, 50, 87, 200),
            limits(1, 99, 87, 200),
        ))
        self.assertIsNone(result.values["five_hour_delta"])
        self.assertIsNone(result.values["five_hour_resets_at"])
        self.assertEqual(result.values["five_hour_resets_at_before"], 50)
        self.assertEqual(result.values["five_hour_resets_at_after"], 99)
        self.assertIs(result.values["five_hour_window_reset"], True)
        self.assertIs(result.values["five_hour_isolated"], False)
        self.assertTrue(result.valid)
        self.assertEqual(result.values["weekly_delta"], 0)
        self.assertTrue(result.values["weekly_isolated"])
        self.assertIs(result.values["weekly_window_reset"], False)

    def test_before_read_failure_does_not_fail_probe(self):
        result = self.probe_with(scripted_windows(None, limits(18, 50, 87, 200)))
        self.assertTrue(result.valid)
        self.assertIsNone(result.values["five_hour_used_percent_before"])
        self.assertEqual(result.values["five_hour_used_percent_after"], 18)
        self.assertIsNone(result.values["five_hour_delta"])
        self.assertIsNone(result.values["five_hour_isolated"])
        self.assertIsNone(result.values["weekly_isolated"])
        self.assertEqual(result.display["cost"], "-")

    def test_after_read_failure_does_not_fail_probe(self):
        result = self.probe_with(scripted_windows(limits(18, 50, 87, 200), None))
        self.assertTrue(result.valid)
        self.assertEqual(result.values["five_hour_used_percent_before"], 18)
        self.assertIsNone(result.values["five_hour_used_percent_after"])
        self.assertIsNone(result.values["five_hour_delta"])
        self.assertIsNone(result.values["five_hour_isolated"])
        self.assertIsNone(result.values["weekly_isolated"])

    def test_foreign_rollout_change_is_not_isolated(self):
        foreign = self.home / "sessions/2026/09/18/rollout-other.jsonl"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("{}\n")
        def runner(command, **kwargs):
            foreign.write_text('{"foreign": true}\n')
            return subprocess.CompletedProcess(command, 0, usage_event(), "")
        result = self.probe_with(steady_window(), runner=runner)
        self.assertTrue(result.valid)
        self.assertEqual(result.values["five_hour_delta"], 0)
        self.assertIs(result.values["five_hour_isolated"], False)

    def test_cost_stays_dash_and_tokens_are_not_converted_to_usd(self):
        result = self.probe_with(scripted_windows(
            limits(10, 1, 80, 2),
            limits(11, 1, 80, 2),
        ))
        self.assertEqual(result.display["cost"], "-")
        self.assertNotIn("cost_usd", result.values)
        self.assertNotIn("usd", json.dumps(result.values))

    def test_weekly_same_window_sample_is_isolated(self):
        result = self.probe_with(steady_window(18, 100, 87, 200))
        self.assertEqual(result.values["weekly_used_percent_before"], 87)
        self.assertEqual(result.values["weekly_used_percent_after"], 87)
        self.assertEqual(result.values["weekly_resets_at"], 200)
        self.assertEqual(result.values["weekly_delta"], 0)
        self.assertTrue(result.values["weekly_isolated"])
        self.assertIs(result.values["weekly_window_reset"], False)

    def test_weekly_zero_delta_is_not_zero_cost(self):
        fields = weekly_budget_fields(
            {"used_percent": 87, "resets_at": 200},
            {"used_percent": 87, "resets_at": 200},
            True,
        )
        self.assertEqual(fields["weekly_delta"], 0)
        self.assertTrue(fields["weekly_isolated"])

    def test_weekly_one_percent_crossing(self):
        result = self.probe_with(scripted_windows(
            limits(18, 50, 87, 200),
            limits(18, 50, 88, 200),
        ))
        self.assertEqual(result.values["weekly_delta"], 1)
        self.assertTrue(result.values["weekly_isolated"])
        self.assertEqual(result.values["five_hour_delta"], 0)
        self.assertTrue(result.values["five_hour_isolated"])
        self.assertEqual(result.display["cost"], "-")

    def test_weekly_reset_invalidates_weekly_only(self):
        result = self.probe_with(scripted_windows(
            limits(18, 50, 99, 200),
            limits(18, 50, 1, 400),
        ))
        self.assertIsNone(result.values["weekly_delta"])
        self.assertIs(result.values["weekly_window_reset"], True)
        self.assertIs(result.values["weekly_isolated"], False)
        self.assertEqual(result.values["five_hour_delta"], 0)
        self.assertTrue(result.values["five_hour_isolated"])
        self.assertIs(result.values["five_hour_window_reset"], False)

    def test_five_hour_reset_leaves_weekly_valid(self):
        result = self.probe_with(scripted_windows(
            limits(99, 50, 87, 200),
            limits(2, 80, 87, 200),
        ))
        self.assertIs(result.values["five_hour_window_reset"], True)
        self.assertIs(result.values["five_hour_isolated"], False)
        self.assertIsNone(result.values["five_hour_delta"])
        self.assertEqual(result.values["weekly_delta"], 0)
        self.assertTrue(result.values["weekly_isolated"])

    def _write_thread(self, cwd, ts):
        path = self.home / "state_5.sqlite"
        with sqlite3.connect(path) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS threads(cwd TEXT, updated_at REAL, updated_at_ms INTEGER)"
            )
            database.execute("INSERT INTO threads VALUES (?, ?, NULL)", (cwd, ts))

    def test_foreign_sqlite_activity_is_not_isolated(self):
        self._write_thread("/tmp/foreign-codex", 1_789_710_000)
        def runner(command, **kwargs):
            self._write_thread("/tmp/foreign-codex", 1_789_710_500)
            return subprocess.CompletedProcess(command, 0, usage_event(), "")
        result = self.probe_with(steady_window(), runner=runner)
        self.assertTrue(result.valid)
        self.assertIs(result.values["five_hour_isolated"], False)
        self.assertIs(result.values["weekly_isolated"], False)

    def test_own_sqlite_thread_for_scratch_stays_isolated(self):
        def runner(command, **kwargs):
            self._write_thread(command[-2], 1_789_710_500)
            return subprocess.CompletedProcess(command, 0, usage_event(), "")
        result = self.probe_with(steady_window(), runner=runner)
        self.assertTrue(result.valid)
        self.assertTrue(result.values["five_hour_isolated"])
        self.assertTrue(result.values["weekly_isolated"])

    def test_unreadable_sqlite_watermark_is_not_isolated(self):
        (self.home / "state_5.sqlite").write_bytes(b"not a sqlite database")
        result = self.probe_with(steady_window())
        self.assertTrue(result.valid)
        self.assertIs(result.values["five_hour_isolated"], False)
        self.assertIs(result.values["weekly_isolated"], False)

    def test_foreign_activity_marks_weekly_non_isolated(self):
        foreign = self.home / "sessions/2026/09/18/rollout-other.jsonl"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("{}\n")
        def runner(command, **kwargs):
            foreign.write_text('{"foreign": true}\n')
            return subprocess.CompletedProcess(command, 0, usage_event(), "")
        result = self.probe_with(steady_window(), runner=runner)
        self.assertIs(result.values["weekly_isolated"], False)
        self.assertIs(result.values["five_hour_isolated"], False)


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
