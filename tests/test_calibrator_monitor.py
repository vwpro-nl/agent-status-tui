import datetime as dt
import inspect
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentstatus.calibrator import cli, monitor
from agentstatus.calibrator.monitor import AGENTS, read_agent_row, read_frame, render_frame
from agentstatus.calibrator.persistence import append_history, save_state


UTC = dt.timezone.utc


def _state(agent, provider, schema, *, mode="explore", next_interval=300, progress=2):
    return {
        "schema": "agent-status-calibrator-state/v2",
        "agent": agent, "provider": provider, "measurement_schema": schema,
        "sampling": {"startup_complete": True, "progress": progress,
                     "next_interval_seconds": next_interval,
                     "last_measured_interval_seconds": next_interval,
                     "last_probe_finished_at": "2026-09-18T10:00:00Z"},
        "baseline": {}, "updated_at": "2026-09-18T10:00:00Z",
        "controller": {"schema": "agent-status-calibrator-controller-state/v2",
                       "mode": mode, "known_good_seconds": None,
                       "confirmed_bad_seconds": None, "candidate_seconds": None,
                       "evidence": {}, "result": None},
    }


def _measurement(agent, provider, schema, *, interval=300, classification="good",
                 display=None, timestamp="2026-09-18T10:00:00Z", next_scheduled_at=None):
    return {
        "event": "measurement", "timestamp": timestamp, "agent": agent, "provider": provider,
        "measurement_schema": schema, "interval_seconds": interval, "valid": True,
        "classification": classification, "values": {}, "display": display or {},
        "error": None, "movement_seconds": 0, "update_baseline": False,
        "next_interval_seconds": interval, "next_movement_seconds": 0,
        "next_scheduled_at": next_scheduled_at,
    }


class MonitorLogicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    # -- structural: no adapter, no provider call path is even reachable --

    def test_monitor_module_imports_no_adapter(self):
        source = inspect.getsource(monitor)
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("adapters import", source)
        self.assertNotIn("CalibratorAdapter(", source)

    def test_reading_a_frame_never_touches_subprocess(self):
        save_state(self.root / "states" / "claude.json",
                   _state("claude", "anthropic", "claude-ccusage/v1"))
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1"))
        with patch("subprocess.Popen", side_effect=AssertionError("monitor must never spawn a process")):
            rows = read_frame(self.root)
        self.assertEqual(len(rows), 3)

    # -- virgin / missing agent --

    def test_agent_with_no_state_file_is_virgin_not_an_error(self):
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.mode, "virgin")
        self.assertIsNone(row.record)

    def test_corrupted_state_file_degrades_to_virgin_not_a_crash(self):
        path = self.root / "states" / "claude.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json")
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.mode, "virgin")

    def test_virgin_row_renders_with_dashes(self):
        row = read_agent_row("claude", "CLAUDE", self.root)
        line = monitor.render_agent_row(row)
        self.assertIn("CLAUDE", line)
        self.assertIn("virgin", line)
        self.assertIn("-", line)
        self.assertNotIn("Traceback", line)

    def test_all_three_agents_virgin_by_default_in_empty_dir(self):
        rows = read_frame(self.root)
        self.assertEqual([r.display_name for r in rows], ["CLAUDE", "CODEX", "GROK"])
        self.assertTrue(all(r.mode == "virgin" for r in rows))

    # -- populated agent --

    def test_populated_agent_shows_latest_measurement(self):
        save_state(self.root / "states" / "claude.json",
                   _state("claude", "anthropic", "claude-ccusage/v1", mode="confirm"))
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1", interval=60,
                                    classification="bad",
                                    display={"c_read": 111, "c_write": 22, "input": 3,
                                             "output": 4, "total": 5, "cost": "$0.0020"},
                                    timestamp="2026-09-18T10:00:00Z",
                                    next_scheduled_at="2026-09-18T10:01:00Z"))
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1", interval=120,
                                    classification="good",
                                    display={"c_read": 999, "c_write": 0, "input": 1,
                                             "output": 2, "total": 3, "cost": "$0.0001"},
                                    timestamp="2026-09-18T10:05:00Z",
                                    next_scheduled_at="2026-09-18T10:07:00Z"))
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.mode, "confirm")
        # the SECOND (latest) record wins, not the first
        self.assertEqual(row.record["classification"], "good")
        self.assertEqual(row.record["display"]["c_read"], 999)
        line = monitor.render_agent_row(row)
        self.assertIn("999", line)
        self.assertIn("confirm", line)
        self.assertIn("good", line)
        self.assertNotIn("111", line)  # the superseded record's value

    def test_missing_metric_fields_render_as_dash(self):
        save_state(self.root / "states" / "grok.json",
                   _state("grok", "xai", "grok-turn-usage/v1"))
        append_history(self.root / "history" / "grok.jsonl",
                       _measurement("grok", "xai", "grok-turn-usage/v1",
                                    display={"c_read": 5}))  # everything else missing
        row = read_agent_row("grok", "GROK", self.root)
        line = monitor.render_agent_row(row)
        self.assertIn("-", line)

    # -- multi-agent / strict separation --

    def test_multiple_agents_state_and_history_stay_separate(self):
        save_state(self.root / "states" / "claude.json",
                   _state("claude", "anthropic", "claude-ccusage/v1", mode="explore"))
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    display={"c_read": 111, "c_write": 0, "input": 1,
                                             "output": 1, "total": 2, "cost": "$0.0001"}))
        save_state(self.root / "states" / "codex.json",
                   _state("codex", "openai", "codex-exec-usage/v1", mode="bracket"))
        append_history(self.root / "history" / "codex.jsonl",
                       _measurement("codex", "openai", "codex-exec-usage/v1",
                                    display={"c_read": 222, "c_write": 0, "input": 2,
                                             "output": 2, "total": 4, "cost": "-"}))
        rows = {row.display_name: row for row in read_frame(self.root)}
        self.assertEqual(rows["CLAUDE"].record["display"]["c_read"], 111)
        self.assertEqual(rows["CODEX"].record["display"]["c_read"], 222)
        self.assertEqual(rows["CLAUDE"].mode, "explore")
        self.assertEqual(rows["CODEX"].mode, "bracket")
        self.assertEqual(rows["GROK"].mode, "virgin")
        # Neither populated agent's data leaks into the untouched third one.
        self.assertIsNone(rows["GROK"].record)

    def test_frame_is_one_line_per_agent_plus_header(self):
        rows = read_frame(self.root)
        frame = render_frame(rows, dt.datetime(2026, 9, 18, 10, 0, tzinfo=UTC))
        lines = frame.splitlines()
        self.assertEqual(len(lines), 5)  # title + header + 3 agent rows
        for name in ("CLAUDE", "CODEX", "GROK"):
            self.assertEqual(sum(1 for line in lines if line.startswith(name)), 1)

    def test_agents_constant_covers_exactly_claude_codex_grok(self):
        self.assertEqual([key for key, _ in AGENTS], ["claude", "codex", "grok"])

    # -- live NEXT, corrected via activity-detected reschedules --

    def _reschedule(self, agent, provider, schema, *, timestamp, scheduled_at):
        return {"event": "activity-detected", "timestamp": timestamp, "agent": agent,
                "provider": provider, "measurement_schema": schema,
                "source": "claude-transcript", "scheduled_at": scheduled_at}

    def _with_claude_state(self):
        save_state(self.root / "states" / "claude.json",
                   _state("claude", "anthropic", "claude-ccusage/v1"))

    def test_next_without_reschedule_uses_measurement_value(self):
        self._with_claude_state()
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    timestamp="2026-09-18T10:00:00Z",
                                    next_scheduled_at="2026-09-18T10:05:00Z"))
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.next_at, dt.datetime(2026, 9, 18, 10, 5, tzinfo=UTC))

    def test_single_reschedule_after_measurement_wins(self):
        self._with_claude_state()
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    timestamp="2026-09-18T10:00:00Z",
                                    next_scheduled_at="2026-09-18T10:05:00Z"))
        append_history(self.root / "history" / "claude.jsonl",
                       self._reschedule("claude", "anthropic", "claude-ccusage/v1",
                                        timestamp="2026-09-18T10:01:00Z",
                                        scheduled_at="2026-09-18T10:09:00Z"))
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.next_at, dt.datetime(2026, 9, 18, 10, 9, tzinfo=UTC))

    def test_multiple_reschedules_use_the_newest(self):
        self._with_claude_state()
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    timestamp="2026-09-18T10:00:00Z",
                                    next_scheduled_at="2026-09-18T10:05:00Z"))
        append_history(self.root / "history" / "claude.jsonl",
                       self._reschedule("claude", "anthropic", "claude-ccusage/v1",
                                        timestamp="2026-09-18T10:01:00Z",
                                        scheduled_at="2026-09-18T10:09:00Z"))
        append_history(self.root / "history" / "claude.jsonl",
                       self._reschedule("claude", "anthropic", "claude-ccusage/v1",
                                        timestamp="2026-09-18T10:02:00Z",
                                        scheduled_at="2026-09-18T10:12:00Z"))
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.next_at, dt.datetime(2026, 9, 18, 10, 12, tzinfo=UTC))

    def test_reschedule_before_latest_measurement_is_ignored(self):
        self._with_claude_state()
        # A reschedule that belongs to an earlier wait (timestamped before,
        # or at, the newest measurement) must not leak into the current
        # NEXT -- the newest measurement always restarts the evaluation.
        append_history(self.root / "history" / "claude.jsonl",
                       self._reschedule("claude", "anthropic", "claude-ccusage/v1",
                                        timestamp="2026-09-18T09:59:00Z",
                                        scheduled_at="2026-09-18T10:30:00Z"))
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    timestamp="2026-09-18T10:00:00Z",
                                    next_scheduled_at="2026-09-18T10:05:00Z"))
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.next_at, dt.datetime(2026, 9, 18, 10, 5, tzinfo=UTC))

    def test_next_restarts_from_a_newer_measurement(self):
        self._with_claude_state()
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    timestamp="2026-09-18T10:00:00Z",
                                    next_scheduled_at="2026-09-18T10:05:00Z"))
        append_history(self.root / "history" / "claude.jsonl",
                       self._reschedule("claude", "anthropic", "claude-ccusage/v1",
                                        timestamp="2026-09-18T10:01:00Z",
                                        scheduled_at="2026-09-18T10:09:00Z"))
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    timestamp="2026-09-18T10:09:00Z",
                                    next_scheduled_at="2026-09-18T10:14:00Z"))
        row = read_agent_row("claude", "CLAUDE", self.root)
        # The old reschedule (before the new measurement) must not apply.
        self.assertEqual(row.next_at, dt.datetime(2026, 9, 18, 10, 14, tzinfo=UTC))

    def test_live_next_does_not_cross_agents(self):
        self._with_claude_state()
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1",
                                    timestamp="2026-09-18T10:00:00Z",
                                    next_scheduled_at="2026-09-18T10:05:00Z"))
        append_history(self.root / "history" / "codex.jsonl",
                       self._reschedule("codex", "openai", "codex-exec-usage/v1",
                                        timestamp="2026-09-18T10:01:00Z",
                                        scheduled_at="2026-09-18T10:59:00Z"))
        row = read_agent_row("claude", "CLAUDE", self.root)
        self.assertEqual(row.next_at, dt.datetime(2026, 9, 18, 10, 5, tzinfo=UTC))


class MonitorCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def test_monitor_once_renders_a_single_frame_without_agent_or_model(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["monitor", "--once", "--state-dir", str(self.root)])
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("CLAUDE", output)
        self.assertIn("CODEX", output)
        self.assertIn("GROK", output)
        self.assertEqual(output.count("virgin"), 3)

    def test_monitor_never_constructs_a_provider_adapter(self):
        with patch.object(cli, "ClaudeCalibratorAdapter", side_effect=AssertionError("no adapter")), \
             patch.object(cli, "CodexCalibratorAdapter", side_effect=AssertionError("no adapter")), \
             patch.object(cli, "GrokCalibratorAdapter", side_effect=AssertionError("no adapter")), \
             redirect_stdout(io.StringIO()):
            rc = cli.main(["monitor", "--once", "--state-dir", str(self.root)])
        self.assertEqual(rc, 0)

    def test_monitor_never_spawns_a_subprocess(self):
        save_state(self.root / "states" / "claude.json",
                   _state("claude", "anthropic", "claude-ccusage/v1"))
        append_history(self.root / "history" / "claude.jsonl",
                       _measurement("claude", "anthropic", "claude-ccusage/v1"))
        with patch("subprocess.Popen", side_effect=AssertionError("monitor must never spawn a process")), \
             redirect_stdout(io.StringIO()):
            rc = cli.main(["monitor", "--once", "--state-dir", str(self.root)])
        self.assertEqual(rc, 0)

    def test_monitor_reflects_a_live_agent_alongside_virgin_ones(self):
        save_state(self.root / "states" / "codex.json",
                   _state("codex", "openai", "codex-exec-usage/v1", mode="bracket"))
        append_history(self.root / "history" / "codex.jsonl",
                       _measurement("codex", "openai", "codex-exec-usage/v1", interval=180,
                                    classification="bad",
                                    display={"c_read": 42, "c_write": 7, "input": 1,
                                             "output": 1, "total": 2, "cost": "-"}))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["monitor", "--once", "--state-dir", str(self.root)])
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("bracket", output)
        self.assertIn("bad", output)
        self.assertIn("42", output)
        self.assertEqual(output.count("virgin"), 2)  # claude + grok


class WaitStatusNoiseTests(unittest.TestCase):
    """The single-agent `run` route: at most one wait-status line per run."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def _fake_calibrator(self, waits, measurements):
        class FakeCalibrator:
            def __init__(self, *a, **kw):
                pass
            def run(self, *, max_measurements=None, emit=None, on_wait=None):
                for status in waits:
                    on_wait(status)
                for record in measurements:
                    emit(record)
                return 0
        return FakeCalibrator

    def test_only_one_wait_status_line_even_with_reschedules(self):
        waits = [
            {"scheduled_at": "2026-09-18T10:01:00Z", "timestamp": "2026-09-18T10:00:00Z", "interval_seconds": 60},
            {"scheduled_at": "2026-09-18T10:02:00Z", "timestamp": "2026-09-18T10:00:30Z", "interval_seconds": 60},
            {"scheduled_at": "2026-09-18T10:03:00Z", "timestamp": "2026-09-18T10:01:30Z", "interval_seconds": 60},
        ]
        measurements = [
            _measurement("claude", "anthropic", "claude-ccusage/v1",
                        display={"c_read": 1, "c_write": 2, "input": 3, "output": 4,
                                 "total": 5, "cost": "$0.0001"}, next_scheduled_at=None),
            _measurement("claude", "anthropic", "claude-ccusage/v1",
                        display={"c_read": 9, "c_write": 8, "input": 7, "output": 6,
                                 "total": 5, "cost": "$0.0002"}, next_scheduled_at=None),
        ]
        buf = io.StringIO()
        with patch.object(cli, "Calibrator", self._fake_calibrator(waits, measurements)), \
             redirect_stdout(buf):
            rc = cli.main(["run", "--agent", "claude", "--state-dir", str(self.root),
                          "--projects-dir", str(self.root / "unused-projects")])
        self.assertEqual(rc, 0)
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        # header + at most one all-dash wait line + 2 measurement lines
        dash_lines = [line for line in lines if "-         -         -         -         -         -" in line]
        self.assertEqual(len(dash_lines), 1)
        self.assertEqual(sum(1 for line in lines if "1         2         3         4         5" in line), 1)
        self.assertEqual(sum(1 for line in lines if "9         8         7         6         5" in line), 1)


class GitCleanlinessTests(unittest.TestCase):
    def test_no_leftover_debug_prints_in_monitor_module(self):
        source = inspect.getsource(monitor)
        self.assertNotIn("print(", source)


if __name__ == "__main__":
    unittest.main()
