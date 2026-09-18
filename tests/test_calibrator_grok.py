import datetime as dt
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

from agentstatus.calibrator import cli
from agentstatus.calibrator.adapters import grok as grok_cal
import signal
import subprocess

from agentstatus.calibrator.adapters.grok import (
    GrokCalibratorAdapter, GrokProbeError, parse_session_turn_usage, parse_turn_usage,
    parse_weekly_billing, run_command, weekly_budget_fields,
)
from agentstatus.calibrator.render import render_header, render_record
from agentstatus import grok_billing
from agentstatus.adapters import grok as grok_status


UTC = dt.timezone.utc
PERIOD_START = "2026-09-05T16:26:02.206145Z"
PERIOD_END = "2026-09-12T16:26:02.206145Z"


def weekly(used=3, start=PERIOD_START, end=PERIOD_END):
    return {"used_percent": used, "period_start": start, "period_end": end}


def scripted(*values):
    remaining = iter(values)
    return lambda: next(remaining)


def turn_event(session_id, **usage_overrides):
    usage = {
        "inputTokens": 30, "outputTokens": 4, "totalTokens": 34,
        "cachedReadTokens": 20, "cacheCreationTokens": 2, "reasoningTokens": 1,
        "modelCalls": 1, "apiDurationMs": 120, "costUsdTicks": 10 ** 10,
        "modelUsage": {"grok-4.6-build": {"inputTokens": 30, "costUsdTicks": 10 ** 10}},
    }
    usage.update(usage_overrides)
    return {
        "timestamp": 1_789_583_765,
        "method": "_x.ai/session/update",
        "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": "turn_completed", "usage": usage},
        },
    }


def write_turn(home: Path, scratch: str, session_id: str, **usage_overrides):
    path = home / "sessions" / quote(scratch, safe="") / session_id / "updates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(turn_event(session_id, **usage_overrides)) + "\n")
    return path


class GrokCalibratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "grok"
        self.home.mkdir()
    def tearDown(self):
        self.temp.cleanup()

    def probe(self, reader, runner=None, session_id="11111111-1111-1111-1111-111111111111", **kwargs):
        def default_runner(command, **kw):
            scratch = command[command.index("--cwd") + 1]
            sid = command[command.index("--session-id") + 1]
            write_turn(self.home, scratch, sid)
            return __import__("subprocess").CompletedProcess(command, 0, "{}\n", "")
        adapter = GrokCalibratorAdapter(
            self.home, model="grok-4.6-build", scratch_root=self.root,
            runner=runner or default_runner, billing_reader=reader,
            session_id_factory=lambda: session_id, **kwargs,
        )
        return adapter.probe()

    def test_probe_command_shape(self):
        captured = []
        def runner(command, **kw):
            captured.append(command)
            scratch = command[command.index("--cwd") + 1]
            sid = command[command.index("--session-id") + 1]
            write_turn(self.home, scratch, sid)
            return __import__("subprocess").CompletedProcess(command, 0, "{}\n", "")
        self.probe(scripted(weekly(), weekly()), runner=runner)
        command = captured[0]
        self.assertEqual(command[0], "grok")
        self.assertIn("--session-id", command)
        self.assertEqual(command[command.index("-p") + 1], "Reply only: OK")
        self.assertEqual(command[command.index("--max-turns") + 1], "1")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--disable-web-search", command)
        self.assertIn("--no-subagents", command)
        self.assertIn("--verbatim", command)
        self.assertNotIn("bypassPermissions", command)

    def test_uses_shared_billing_helpers_and_never_reads_refresh_token(self):
        source = inspect.getsource(grok_cal)
        self.assertIn("select_bearer", source)
        self.assertIn("billing_request", source)
        self.assertNotIn("from ...adapters import grok", source)
        self.assertNotIn('.get("refresh_token")', inspect.getsource(grok_billing.select_bearer))
        self.assertIs(grok_status._select_bearer, grok_billing.select_bearer)
        self.assertIs(grok_status._billing_request, grok_billing.billing_request)

    def test_weekly_same_period_zero_delta(self):
        result = self.probe(scripted(weekly(3), weekly(3)))
        self.assertTrue(result.valid)
        self.assertEqual(result.values["weekly_used_percent_before"], 3)
        self.assertEqual(result.values["weekly_used_percent_after"], 3)
        self.assertEqual(result.values["weekly_delta"], 0)
        self.assertTrue(result.values["weekly_isolated"])
        self.assertIs(result.values["weekly_window_reset"], False)
        self.assertEqual(result.values["weekly_period_start"], "2026-09-05T16:26:02.206145Z")
        self.assertEqual(result.display["cost"][0], "$")

    def test_weekly_one_percent_crossing(self):
        result = self.probe(scripted(weekly(3), weekly(4)))
        self.assertEqual(result.values["weekly_delta"], 1)
        self.assertTrue(result.values["weekly_isolated"])
        self.assertEqual(result.display["cost"], "$1.0000")

    def test_weekly_period_reset_invalidates_sample(self):
        result = self.probe(scripted(
            weekly(99, PERIOD_START, PERIOD_END),
            weekly(1, "2026-09-12T16:26:02.206145Z", "2026-09-19T16:26:02.206145Z"),
        ))
        self.assertIsNone(result.values["weekly_delta"])
        self.assertIs(result.values["weekly_window_reset"], True)
        self.assertIs(result.values["weekly_isolated"], False)
        self.assertTrue(result.valid)

    def test_before_billing_failure_does_not_fail_probe(self):
        result = self.probe(scripted(None, weekly(3)))
        self.assertTrue(result.valid)
        self.assertIsNone(result.values["weekly_used_percent_before"])
        self.assertEqual(result.values["weekly_used_percent_after"], 3)
        self.assertIsNone(result.values["weekly_isolated"])

    def test_after_billing_failure_does_not_fail_probe(self):
        result = self.probe(scripted(weekly(3), None))
        self.assertTrue(result.valid)
        self.assertEqual(result.values["weekly_used_percent_before"], 3)
        self.assertIsNone(result.values["weekly_delta"])
        self.assertIsNone(result.values["weekly_isolated"])

    def test_foreign_session_update_is_not_isolated(self):
        foreign = self.home / "sessions" / "%2Fother" / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" / "updates.jsonl"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("{}\n")
        def runner(command, **kw):
            scratch = command[command.index("--cwd") + 1]
            sid = command[command.index("--session-id") + 1]
            write_turn(self.home, scratch, sid)
            foreign.write_text('{"foreign": true}\n')
            return __import__("subprocess").CompletedProcess(command, 0, "{}\n", "")
        result = self.probe(scripted(weekly(), weekly()), runner=runner)
        self.assertTrue(result.valid)
        self.assertIs(result.values["weekly_isolated"], False)

    def test_own_session_files_are_not_foreign(self):
        result = self.probe(scripted(weekly(), weekly()))
        self.assertTrue(result.values["weekly_isolated"])

    def test_turn_completed_usage_and_cost_ticks(self):
        usage = parse_turn_usage(turn_event("sid", costUsdTicks=1), "sid")
        self.assertEqual(usage["cost_usd_ticks"], 1)
        self.assertEqual(usage["cost_usd"], float(Decimal(1) / Decimal(10 ** 10)))
        self.assertEqual(usage["cache_read_tokens"], 20)
        self.assertEqual(usage["cache_creation_tokens"], 2)
        other = parse_turn_usage(turn_event("other"), "sid")
        self.assertIsNone(other)

    def test_display_mapping_and_header(self):
        result = self.probe(scripted(weekly(), weekly()))
        self.assertEqual(result.display["c_read"], 20)
        self.assertEqual(result.display["c_write"], 2)
        self.assertEqual(result.display["input"], 30)
        self.assertEqual(result.display["output"], 4)
        self.assertEqual(result.display["total"], 34)
        self.assertEqual(result.display["cost"], "$1.0000")
        header = render_header()
        self.assertEqual(
            header,
            "TIME      AGENT    INTERVAL  C.READ    C.WRITE   IN        OUT       TOTAL     COST      NEXT",
        )
        line = render_record({"timestamp": "2026-09-18T06:00:00Z",
                              "interval_seconds": 60, "next_movement_seconds": 0,
                              "display": result.display}, "GROK")
        self.assertIn("20        2         30        4         34        $1.0000", line)

    def test_model_probe_failure(self):
        def runner(command, **kw):
            return __import__("subprocess").CompletedProcess(command, 7, "SECRET", "SECRET")
        result = self.probe(scripted(weekly(), weekly()), runner=runner)
        self.assertFalse(result.valid)
        self.assertIn("exited 7", result.error)
        self.assertNotIn("SECRET", result.error)
        self.assertEqual(result.values["weekly_delta"], 0)

    def test_usage_failure_does_not_invent_tokens(self):
        def runner(command, **kw):
            return __import__("subprocess").CompletedProcess(command, 0, "{}\n", "")
        result = self.probe(scripted(weekly(5), weekly(5)), runner=runner)
        self.assertFalse(result.valid)
        self.assertIn("no usable turn usage", result.error)
        self.assertNotIn("input_tokens", result.values)
        self.assertEqual(result.values["weekly_used_percent_before"], 5)

    def test_weekly_not_derived_from_tokens_or_usd(self):
        result = self.probe(scripted(weekly(3), weekly(3)))
        self.assertEqual(result.values["weekly_delta"], 0)
        self.assertEqual(result.values["cost_usd"], 1.0)
        self.assertNotEqual(result.values["weekly_delta"], result.values["cost_usd"])
        self.assertNotEqual(result.values["weekly_delta"], result.values["total_tokens"])

    def test_zero_delta_fields_are_censored_not_zero_cost(self):
        fields = weekly_budget_fields(weekly(3), weekly(3), True)
        self.assertEqual(fields["weekly_delta"], 0)
        self.assertTrue(fields["weekly_isolated"])

    def test_parse_weekly_billing_keeps_whole_percent(self):
        parsed = parse_weekly_billing({
            "config": {
                "creditUsagePercent": 3,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-09-05T16:26:02.206145+00:00",
                    "end": "2026-09-12T16:26:02.206145+00:00",
                },
            }
        })
        self.assertEqual(parsed["used_percent"], 3)
        self.assertIsInstance(parsed["used_percent"], int)

    def test_cli_selects_grok_without_probe(self):
        fake = Mock(display_name="GROK")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "GrokCalibratorAdapter", return_value=fake) as constructor, \
             redirect_stdout(io.StringIO()):
            result = cli.main(["status", "--agent", "grok", "--state-dir", directory])
        self.assertEqual(result, 0)
        constructor.assert_called_once()

    def test_optional_cache_and_reasoning_default_to_zero(self):
        event = turn_event("sid")
        del event["params"]["update"]["usage"]["cachedReadTokens"]
        del event["params"]["update"]["usage"]["cacheCreationTokens"]
        del event["params"]["update"]["usage"]["reasoningTokens"]
        usage = parse_turn_usage(event, "sid")
        self.assertEqual(usage["cache_read_tokens"], 0)
        self.assertEqual(usage["cache_creation_tokens"], 0)
        self.assertEqual(usage["reasoning_tokens"], 0)
        self.assertEqual(usage["input_tokens"], 30)

    def test_missing_cost_ticks_is_not_zero_dollars(self):
        event = turn_event("sid")
        del event["params"]["update"]["usage"]["costUsdTicks"]
        usage = parse_turn_usage(event, "sid")
        self.assertIsNotNone(usage)
        self.assertNotIn("cost_usd", usage)
        self.assertNotIn("cost_usd_ticks", usage)

        def runner(command, **kw):
            scratch = command[command.index("--cwd") + 1]
            sid = command[command.index("--session-id") + 1]
            payload = turn_event(sid)
            del payload["params"]["update"]["usage"]["costUsdTicks"]
            path = self.home / "sessions" / quote(scratch, safe="") / sid / "updates.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload) + "\n")
            return subprocess.CompletedProcess(command, 0, "{}\n", "")
        result = self.probe(scripted(weekly(), weekly()), runner=runner)
        self.assertTrue(result.valid)
        self.assertEqual(result.display["cost"], "-")
        self.assertNotIn("cost_usd", result.values)

    def test_negative_optional_field_is_invalid(self):
        self.assertIsNone(parse_turn_usage(turn_event("sid", reasoningTokens=-1), "sid"))

    def test_timeout_kills_process_group(self):
        class Process:
            pid = 4242
            calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("grok", timeout)
                return "", ""
        with patch("agentstatus.calibrator.adapters.grok.subprocess.Popen", return_value=Process()), \
             patch("agentstatus.calibrator.adapters.grok.os.killpg") as killpg:
            with self.assertRaises(GrokProbeError) as raised:
                run_command(["grok", "-p", "Reply only: OK"], timeout=1)
        self.assertIn("did not complete", str(raised.exception))
        killpg.assert_called_once_with(4242, signal.SIGKILL)

    def test_assess_ok_or_fail(self):
        adapter = GrokCalibratorAdapter(self.home)
        from agentstatus.calibrator.model import Observation
        self.assertEqual(adapter.assess(Observation(True, "a", {"total_tokens": 1}), {}).classification, "ok")
        failed = adapter.assess(Observation(False, "b", error="x"), {"kept": 1})
        self.assertEqual(failed.classification, "fail")
        self.assertFalse(failed.update_baseline)


if __name__ == "__main__":
    unittest.main()
