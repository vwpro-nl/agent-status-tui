"""Keepalive tests.

Deliberately self-contained: nothing here imports from agentstatus.calibrator
or from tests/test_calibrator.py, so this whole module can be collected and
run even if the calibrator package did not exist.
"""

import datetime as dt
import inspect
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentstatus import keepalive
from agentstatus.keepalive import cli as keepalive_cli
from agentstatus.keepalive import core as keepalive_core
from agentstatus.keepalive import providers as keepalive_providers
from agentstatus.keepalive.core import INTERVAL_SECONDS, KeepaliveAgent, initial_state, iso
from agentstatus.keepalive.persistence import STATE_SCHEMA, load_state, save_state
from agentstatus.keepalive.providers import ClaudeKeepalive, CodexKeepalive, GrokKeepalive, PingResult

UTC = dt.timezone.utc
KEEPALIVE_PACKAGE_ROOT = Path(keepalive.__file__).parent

# 3001 is a deliberate, pinned system parameter -- never round it to 3000.
EXPECTED_INTERVAL_SECONDS = 3001


class Clock:
    """A tiny, self-contained fake clock -- no dependency on any other
    test module in this project."""

    def __init__(self):
        self.value = dt.datetime(2026, 9, 18, 6, 0, tzinfo=UTC)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += dt.timedelta(seconds=seconds)


class FakeProvider:
    """A minimal, self-contained stand-in for a real provider ping/activity
    check -- implements exactly the duck-typed protocol KeepaliveAgent needs
    (key, display_name, detect_activity(), ping()), nothing more.
    """

    key = "test-agent"
    display_name = "TEST"

    def __init__(self, clock, *, activity_at=None, results=None):
        self.clock = clock
        self.activity_at = activity_at
        self.results = iter(results if results is not None else [PingResult(True, None)])
        self.pings = 0

    def detect_activity(self):
        return self.activity_at

    def ping(self):
        self.pings += 1
        return next(self.results, PingResult(True, None))


class ScriptedActivityProvider(FakeProvider):
    """FakeProvider whose detect_activity() replays a fixed, non-moving
    script -- safe to re-check any number of times without drifting.
    """

    def __init__(self, clock, script, **kwargs):
        super().__init__(clock, **kwargs)
        self.script = iter(script)

    def detect_activity(self):
        return next(self.script)


class KeepaliveCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, provider=None, state_name="agent.json", check_interval=60):
        return KeepaliveAgent(provider or FakeProvider(self.clock), self.root / state_name,
                              clock=self.clock, sleeper=self.clock.sleep, check_interval=check_interval)

    def test_interval_is_pinned_at_3001_seconds_not_rounded_to_3000(self):
        self.assertEqual(INTERVAL_SECONDS, EXPECTED_INTERVAL_SECONDS)
        self.assertNotEqual(INTERVAL_SECONDS, 3000)

    def test_six_cycles_equal_18006_seconds(self):
        # 6 * 3001 = 18006s = 5h00m06s -- the whole point of 3001 over 3000.
        self.assertEqual(INTERVAL_SECONDS * 6, 18006)
        self.assertEqual(18006, 5 * 3600 + 6)

    def test_exact_interval_is_enforced(self):
        agent = self.agent()
        start = self.clock()
        waits = []
        agent.run(max_pings=1, on_wait=waits.append)
        scheduled = dt.datetime.fromisoformat(waits[0]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual((scheduled - start).total_seconds(), EXPECTED_INTERVAL_SECONDS)

    def test_exit_zero_is_ok_nonzero_is_fail(self):
        provider = FakeProvider(self.clock, results=[PingResult(True, None), PingResult(False, "boom")])
        agent = self.agent(provider)
        statuses = []
        agent.run(max_pings=2, on_status=statuses.append)
        self.assertEqual(statuses[0]["status"], "ok")
        self.assertIsNone(statuses[0]["error"])
        self.assertEqual(statuses[1]["status"], "fail")
        self.assertEqual(statuses[1]["error"], "boom")

    def test_ping_resets_own_deadline(self):
        agent = self.agent()
        waits = []
        statuses = []
        agent.run(max_pings=2, on_wait=waits.append, on_status=statuses.append)
        first_finished = dt.datetime.fromisoformat(statuses[0]["finished_at"].replace("Z", "+00:00"))
        second_scheduled = dt.datetime.fromisoformat(waits[1]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual((second_scheduled - first_finished).total_seconds(), EXPECTED_INTERVAL_SECONDS)

    def test_failed_ping_still_waits_a_full_interval_before_retry(self):
        provider = FakeProvider(self.clock, results=[PingResult(False, "boom"), PingResult(True, None)])
        agent = self.agent(provider)
        statuses = []
        waits = []
        agent.run(max_pings=2, on_status=statuses.append, on_wait=waits.append)
        self.assertEqual(statuses[0]["status"], "fail")
        self.assertEqual(statuses[1]["status"], "ok")
        first_finished = dt.datetime.fromisoformat(statuses[0]["finished_at"].replace("Z", "+00:00"))
        second_scheduled = dt.datetime.fromisoformat(waits[1]["scheduled_at"].replace("Z", "+00:00"))
        # No fast retry: the next attempt is scheduled a full fresh interval
        # after the *failed* ping finished, exactly as after a success.
        self.assertEqual((second_scheduled - first_finished).total_seconds(), EXPECTED_INTERVAL_SECONDS)

    def test_three_independent_agents_activity_shifts_only_the_affected_one(self):
        claude_clock = Clock()
        codex_clock = Clock()
        grok_clock = Clock()
        claude_activity_at = claude_clock.value + dt.timedelta(seconds=500)
        claude_provider = ScriptedActivityProvider(claude_clock, [claude_activity_at] * 10)
        codex_provider = FakeProvider(codex_clock, activity_at=None)
        grok_provider = FakeProvider(grok_clock, activity_at=None)

        claude_agent = KeepaliveAgent(claude_provider, self.root / "claude.json",
                                      clock=claude_clock, sleeper=claude_clock.sleep, check_interval=4000)
        codex_agent = KeepaliveAgent(codex_provider, self.root / "codex.json",
                                     clock=codex_clock, sleeper=codex_clock.sleep, check_interval=4000)
        grok_agent = KeepaliveAgent(grok_provider, self.root / "grok.json",
                                    clock=grok_clock, sleeper=grok_clock.sleep, check_interval=4000)

        claude_start, codex_start, grok_start = claude_clock.value, codex_clock.value, grok_clock.value
        claude_state, _ = claude_agent.load()
        codex_state, _ = codex_agent.load()
        grok_state, _ = grok_agent.load()

        claude_wait = claude_agent._wait(claude_state)
        codex_wait = codex_agent._wait(codex_state)
        grok_wait = grok_agent._wait(grok_state)

        claude_deadline = dt.datetime.fromisoformat(claude_wait["scheduled_at"].replace("Z", "+00:00"))
        codex_deadline = dt.datetime.fromisoformat(codex_wait["scheduled_at"].replace("Z", "+00:00"))
        grok_deadline = dt.datetime.fromisoformat(grok_wait["scheduled_at"].replace("Z", "+00:00"))

        self.assertEqual(claude_deadline, claude_activity_at + dt.timedelta(seconds=INTERVAL_SECONDS))
        self.assertNotEqual(claude_deadline, claude_start + dt.timedelta(seconds=INTERVAL_SECONDS))
        self.assertEqual(codex_deadline, codex_start + dt.timedelta(seconds=INTERVAL_SECONDS))
        self.assertEqual(grok_deadline, grok_start + dt.timedelta(seconds=INTERVAL_SECONDS))

    def test_activity_older_than_floor_discovered_mid_wait_never_pulls_deadline_earlier(self):
        # Exact reproduction of the analyzed race: at the very start of the
        # wait no activity is known yet (None); partway through, a
        # timestamp surfaces that IS newer than that prior "None", but is
        # still older than floor (last_ping_finished_at). Before the fix,
        # comparing against the prior known-activity (None) instead of the
        # anchor let this pull the deadline to `stale_activity + interval`,
        # earlier than the correct `floor + interval`.
        floor_time = self.clock.value
        stale_activity = floor_time - dt.timedelta(seconds=500)
        provider = ScriptedActivityProvider(self.clock, [None] + [stale_activity] * 10)
        state_path = self.root / "agent.json"
        save_state(state_path, {**initial_state(provider), "last_ping_finished_at": iso(floor_time)})
        agent = KeepaliveAgent(provider, state_path, clock=self.clock, sleeper=self.clock.sleep,
                               check_interval=4000)
        waits = []
        state, _ = agent.load()
        result = agent._wait(state, on_wait=waits.append)

        correct_deadline = floor_time + dt.timedelta(seconds=INTERVAL_SECONDS)
        scheduled = dt.datetime.fromisoformat(result["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(scheduled, correct_deadline)
        self.assertGreater(scheduled, stale_activity + dt.timedelta(seconds=INTERVAL_SECONDS),
                           "must not have been pulled forward to stale_activity + interval")
        # No emitted reschedule ever proposed an earlier deadline either.
        for status in waits:
            emitted = dt.datetime.fromisoformat(status["scheduled_at"].replace("Z", "+00:00"))
            self.assertGreaterEqual(emitted, correct_deadline)

    def test_activity_genuinely_newer_than_anchor_still_reschedules_later(self):
        # The other half of the same contract: real newer activity must
        # still push the deadline forward, exactly as before the fix.
        floor_time = self.clock.value
        newer_activity = floor_time + dt.timedelta(seconds=500)
        provider = ScriptedActivityProvider(self.clock, [None] + [newer_activity] * 10)
        state_path = self.root / "agent.json"
        save_state(state_path, {**initial_state(provider), "last_ping_finished_at": iso(floor_time)})
        agent = KeepaliveAgent(provider, state_path, clock=self.clock, sleeper=self.clock.sleep,
                               check_interval=4000)
        state, _ = agent.load()
        result = agent._wait(state)
        scheduled = dt.datetime.fromisoformat(result["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(scheduled, newer_activity + dt.timedelta(seconds=INTERVAL_SECONDS))
        self.assertGreater(scheduled, floor_time + dt.timedelta(seconds=INTERVAL_SECONDS))

    def test_restart_resumes_persisted_floor_not_a_fresh_interval(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        ping_finished = self.clock() - dt.timedelta(seconds=1000)
        save_state(state_path, {**initial_state(provider), "last_ping_finished_at": iso(ping_finished)})
        agent = KeepaliveAgent(provider, state_path, clock=self.clock, sleeper=self.clock.sleep, check_interval=60)
        waits = []
        agent.run(max_pings=1, on_wait=waits.append)
        scheduled = dt.datetime.fromisoformat(waits[0]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(scheduled, ping_finished + dt.timedelta(seconds=INTERVAL_SECONDS))

    def test_restart_pings_immediately_when_already_overdue(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        stale = self.clock() - dt.timedelta(seconds=INTERVAL_SECONDS + 500)
        save_state(state_path, {**initial_state(provider), "last_ping_finished_at": iso(stale)})
        agent = KeepaliveAgent(provider, state_path, clock=self.clock, sleeper=self.clock.sleep, check_interval=60)
        start = self.clock()
        statuses = []
        agent.run(max_pings=1, on_status=statuses.append)
        finished = dt.datetime.fromisoformat(statuses[0]["finished_at"].replace("Z", "+00:00"))
        self.assertLess((finished - start).total_seconds(), 5)
        self.assertEqual(provider.pings, 1)

    def test_ping_once_does_not_wait(self):
        def forbidden_sleep(seconds):
            raise AssertionError("ping_once must not sleep/wait at all")
        provider = FakeProvider(self.clock)
        agent = KeepaliveAgent(provider, self.root / "agent.json",
                               clock=self.clock, sleeper=forbidden_sleep, check_interval=60)
        record = agent.ping_once()
        self.assertEqual(record["status"], "ok")
        self.assertEqual(provider.pings, 1)

    def test_ping_once_never_calls_wait(self):
        provider = FakeProvider(self.clock)
        agent = self.agent(provider)
        original_wait = agent._wait
        calls = {"count": 0}
        def spying_wait(*args, **kwargs):
            calls["count"] += 1
            return original_wait(*args, **kwargs)
        agent._wait = spying_wait
        agent.ping_once()
        self.assertEqual(calls["count"], 0)

    def test_ping_once_pings_exactly_once_and_updates_state_normally(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        agent = KeepaliveAgent(provider, state_path, clock=self.clock, sleeper=self.clock.sleep, check_interval=60)
        statuses = []
        agent.ping_once(on_status=statuses.append)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(provider.pings, 1)
        persisted = load_state(state_path)
        self.assertEqual(persisted["last_status"], "ok")
        self.assertIsNotNone(persisted["last_ping_finished_at"])

    def test_persisted_state_carries_no_token_cache_or_cost_fields(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        agent = KeepaliveAgent(provider, state_path, clock=self.clock, sleeper=self.clock.sleep, check_interval=60)
        agent.run(max_pings=1)
        persisted = load_state(state_path)
        self.assertEqual(persisted["schema"], STATE_SCHEMA)
        self.assertEqual(set(persisted.keys()), {
            "schema", "agent", "last_ping_finished_at", "last_status", "last_error", "updated_at",
        })

    def test_identity_mismatch_on_resume_is_rejected(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        save_state(state_path, {**initial_state(provider), "agent": "someone-else"})
        agent = KeepaliveAgent(provider, state_path, clock=self.clock, sleeper=self.clock.sleep)
        with self.assertRaises(Exception):
            agent.load()


class KeepaliveProviderTests(unittest.TestCase):
    """The actual command each provider builds, and how each detects
    activity -- verified without ever running a real subprocess."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_claude_command_is_minimal_non_interactive_reply_only_ok(self):
        seen = {}
        def fake_runner(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return PingResult(True, None)
        provider = ClaudeKeepalive(self.home, model="sonnet", runner=fake_runner)
        result = provider.ping()
        self.assertTrue(result.ok)
        self.assertEqual(seen["argv"], [
            "claude", "--safe-mode", "--exclude-dynamic-system-prompt-sections",
            "--tools", "", "--model", "sonnet", "-p", "Reply only: OK",
        ])

    def test_codex_command_is_minimal_non_interactive_reply_only_ok(self):
        seen = {}
        def fake_runner(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return PingResult(True, None)
        provider = CodexKeepalive(self.home, model="codex-fixture", runner=fake_runner)
        result = provider.ping()
        self.assertTrue(result.ok)
        argv = seen["argv"]
        self.assertEqual(argv[0], "codex")
        self.assertEqual(argv[1:8], ["exec", "--sandbox", "read-only", "--skip-git-repo-check",
                                     "--json", "--color", "never"])
        self.assertEqual(argv[8:10], ["-m", "codex-fixture"])
        self.assertEqual(argv[10], "-C")
        self.assertEqual(argv[-1], "Reply only: OK")
        self.assertEqual(seen["kwargs"]["env"]["CODEX_HOME"], str(self.home))

    def test_grok_command_is_minimal_non_interactive_reply_only_ok(self):
        seen = {}
        def fake_runner(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return PingResult(True, None)
        provider = GrokKeepalive(self.home, model="grok-fixture", runner=fake_runner)
        result = provider.ping()
        self.assertTrue(result.ok)
        argv = seen["argv"]
        self.assertEqual(argv[0], "grok")
        self.assertEqual(argv[1:3], ["-m", "grok-fixture"])
        self.assertIn("--sandbox", argv)
        self.assertIn("read-only", argv)
        self.assertIn("--max-turns", argv)
        self.assertEqual(argv[-1], "Reply only: OK")
        self.assertEqual(seen["kwargs"]["env"]["GROK_HOME"], str(self.home))

    # -- Live bug regression: keepalive must never force a model choice.
    # Confirmed live failure: Codex received "-m unused" and rejected it
    # outright ("Model metadata for `unused` not found"). No provider may
    # ever be given a fabricated/sentinel model string, and none may be
    # given -m/--model at all unless the caller explicitly asked for one.

    def test_default_codex_command_has_no_model_flag_and_no_unused_sentinel(self):
        seen = {}
        def fake_runner(argv, **kwargs):
            seen["argv"] = argv
            return PingResult(True, None)
        provider = CodexKeepalive(self.home, runner=fake_runner)  # no model given
        provider.ping()
        argv = seen["argv"]
        self.assertNotIn("-m", argv)
        self.assertNotIn("unused", argv)
        self.assertEqual(argv[0:8], ["codex", "exec", "--sandbox", "read-only",
                                     "--skip-git-repo-check", "--json", "--color", "never"])
        self.assertEqual(argv[8], "-C")
        self.assertEqual(argv[-1], "Reply only: OK")
        self.assertEqual(len(argv), 11)  # exactly: the 8 fixed flags + -C <scratch> + prompt

    def test_default_claude_command_has_no_model_flag(self):
        seen = {}
        def fake_runner(argv, **kwargs):
            seen["argv"] = argv
            return PingResult(True, None)
        provider = ClaudeKeepalive(self.home, runner=fake_runner)  # no model given
        provider.ping()
        argv = seen["argv"]
        self.assertNotIn("--model", argv)
        self.assertNotIn("unused", argv)
        self.assertEqual(argv, [
            "claude", "--safe-mode", "--exclude-dynamic-system-prompt-sections",
            "--tools", "", "-p", "Reply only: OK",
        ])

    def test_default_grok_command_has_no_model_flag(self):
        seen = {}
        def fake_runner(argv, **kwargs):
            seen["argv"] = argv
            return PingResult(True, None)
        provider = GrokKeepalive(self.home, runner=fake_runner)  # no model given
        provider.ping()
        argv = seen["argv"]
        self.assertNotIn("-m", argv)
        self.assertNotIn("unused", argv)
        self.assertEqual(argv[0], "grok")
        self.assertEqual(argv[-1], "Reply only: OK")

    def test_explicit_model_is_still_added_when_the_caller_asks_for_one(self):
        for provider_cls, flag in (
            (ClaudeKeepalive, "--model"),
            (CodexKeepalive, "-m"),
            (GrokKeepalive, "-m"),
        ):
            with self.subTest(provider=provider_cls.__name__):
                seen = {}
                def fake_runner(argv, **kwargs):
                    seen["argv"] = argv
                    return PingResult(True, None)
                provider = provider_cls(self.home, model="explicit-model", runner=fake_runner)
                provider.ping()
                argv = seen["argv"]
                self.assertIn(flag, argv)
                self.assertEqual(argv[argv.index(flag) + 1], "explicit-model")

    def test_no_unused_sentinel_string_appears_anywhere_in_keepalive_source(self):
        # AST-based, not a raw text scan: a `#` comment is allowed to
        # *mention* the historical "unused" bug in prose (explaining why the
        # guard exists) -- what must never exist is the literal value used
        # as an actual default/argument/constant anywhere in the code.
        import ast
        for path in KEEPALIVE_PACKAGE_ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "unused":
                    self.fail(f"{path.name} uses the literal sentinel string \"unused\" as a value")

    def test_claude_activity_is_newest_projects_transcript_mtime(self):
        projects = self.home / "projects"
        projects.mkdir(parents=True)
        old = projects / "old.jsonl"
        new = projects / "sub" / "new.jsonl"
        new.parent.mkdir(parents=True)
        old.write_text("{}")
        new.write_text("{}")
        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(new, (1_800_000_000, 1_800_000_000))
        provider = ClaudeKeepalive(self.home)
        activity = provider.detect_activity()
        self.assertEqual(activity, dt.datetime.fromtimestamp(1_800_000_000, UTC))

    def test_codex_activity_is_newest_rollout_mtime(self):
        sessions = self.home / "sessions"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-a.jsonl"
        rollout.write_text("{}")
        os.utime(rollout, (1_750_000_000, 1_750_000_000))
        (sessions / "unrelated.txt").write_text("x")
        os.utime(sessions / "unrelated.txt", (1_900_000_000, 1_900_000_000))
        provider = CodexKeepalive(self.home)
        activity = provider.detect_activity()
        self.assertEqual(activity, dt.datetime.fromtimestamp(1_750_000_000, UTC))

    def test_grok_activity_is_newest_session_file_mtime(self):
        sessions = self.home / "sessions" / "abc"
        sessions.mkdir(parents=True)
        summary = sessions / "summary.json"
        summary.write_text("{}")
        os.utime(summary, (1_760_000_000, 1_760_000_000))
        provider = GrokKeepalive(self.home)
        activity = provider.detect_activity()
        self.assertEqual(activity, dt.datetime.fromtimestamp(1_760_000_000, UTC))

    def test_activity_is_none_when_no_session_directory_exists(self):
        self.assertIsNone(ClaudeKeepalive(self.home).detect_activity())
        self.assertIsNone(CodexKeepalive(self.home).detect_activity())
        self.assertIsNone(GrokKeepalive(self.home).detect_activity())

    def test_nonzero_exit_is_fail_with_short_error(self):
        result = keepalive_providers.run_command(
            [sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_zero_exit_is_ok(self):
        result = keepalive_providers.run_command([sys.executable, "-c", "pass"])
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)


class KeepaliveCliTests(unittest.TestCase):
    def test_cli_without_model_flag_builds_providers_with_no_model_at_all(self):
        # The exact live-bug scenario: `agent-status-tui keepalive` with no
        # --model must not silently default any provider to a sentinel
        # ("unused") or a hardcoded name -- model stays None end-to-end.
        args = keepalive_cli.build_parser().parse_args(["--state-dir", "/tmp/does-not-matter"])
        self.assertIsNone(args.model)
        for key in ("claude", "codex", "grok"):
            provider = keepalive_cli._build_provider(key, args)
            self.assertIsNone(provider.model)

    def test_once_pings_each_agent_exactly_once_immediately_with_zero_waits(self):
        calls = {"claude": 0, "codex": 0, "grok": 0}

        def make_runner(agent_key):
            def runner(argv, **kwargs):
                calls[agent_key] += 1
                return PingResult(True, None)
            return runner

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with patch.object(keepalive_cli, "ClaudeKeepalive",
                              side_effect=lambda *a, **kw: ClaudeKeepalive(*a, **{**kw, "runner": make_runner("claude")})), \
                 patch.object(keepalive_cli, "CodexKeepalive",
                              side_effect=lambda *a, **kw: CodexKeepalive(*a, **{**kw, "runner": make_runner("codex")})), \
                 patch.object(keepalive_cli, "GrokKeepalive",
                              side_effect=lambda *a, **kw: GrokKeepalive(*a, **{**kw, "runner": make_runner("grok")})), \
                 redirect_stdout(io.StringIO()) as out:
                start = time.monotonic()
                rc = keepalive_cli.main(["--once", "--state-dir", str(state_dir), "--model", "fixture"])
                elapsed = time.monotonic() - start

        self.assertEqual(rc, 0)
        self.assertEqual(calls, {"claude": 1, "codex": 1, "grok": 1})
        self.assertLess(elapsed, 5, "must return immediately, never wait for the 3001s deadline")
        rendered = out.getvalue()
        self.assertIn("AGENT", rendered)
        self.assertIn("CLAUDE", rendered)
        self.assertIn("CODEX", rendered)
        self.assertIn("GROK", rendered)
        self.assertIn("ok", rendered)


class KeepaliveCliDispatchTests(unittest.TestCase):
    """--once and continuous mode must route through different core methods."""

    def test_once_calls_ping_once_never_run_or_wait(self):
        fixed_record = {"agent": "x", "finished_at": iso(dt.datetime.now(UTC)), "status": "ok", "error": None}
        with patch.object(keepalive_core.KeepaliveAgent, "ping_once", return_value=fixed_record) as ping_once, \
             patch.object(keepalive_core.KeepaliveAgent, "run") as run, \
             patch.object(keepalive_core.KeepaliveAgent, "_wait") as wait, \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--once", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(ping_once.call_count, 3)
        run.assert_not_called()
        wait.assert_not_called()

    def test_continuous_mode_calls_run_never_ping_once(self):
        with patch.object(keepalive_core.KeepaliveAgent, "run", return_value=0) as run, \
             patch.object(keepalive_core.KeepaliveAgent, "ping_once") as ping_once, \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 3)
        ping_once.assert_not_called()

    def test_service_mode_calls_run_via_supervised_wrapper_for_all_three(self):
        with patch.object(keepalive_core.KeepaliveAgent, "run", return_value=0) as run, \
             patch.object(keepalive_core.KeepaliveAgent, "ping_once") as ping_once, \
             patch.object(keepalive_cli.time, "sleep"), \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = keepalive_cli.main(["--service", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 3)
        ping_once.assert_not_called()


class KeepaliveSupervisedRunTests(unittest.TestCase):
    """A provider/scheduler failure must not silently end that agent's loop
    or take the whole process down -- only a normal `PingResult(False, ...)`
    reaches KeepaliveAgent.ping() (already covered elsewhere); this covers
    the _supervised_run wrapper's handling of anything unexpected."""

    def test_survives_an_unexpected_exception_and_retries_after_backoff(self):
        calls = {"n": 0}

        class FlakyAgent:
            def run(self, on_status=None, on_wait=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")
                return 0  # succeeds on retry

        sleeps = []
        with redirect_stderr(io.StringIO()):
            keepalive_cli._supervised_run(FlakyAgent(), "test", sleeper=sleeps.append)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sleeps, [keepalive_cli.RESTART_BACKOFF_SECONDS])

    def test_logs_the_exception_with_the_agent_key_to_stderr(self):
        class FlakyAgent:
            def __init__(self):
                self.calls = 0

            def run(self, on_status=None, on_wait=None):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("kaboom")
                return 0

        buf = io.StringIO()
        with redirect_stderr(buf):
            keepalive_cli._supervised_run(FlakyAgent(), "codex", sleeper=lambda seconds: None)
        output = buf.getvalue()
        self.assertIn("CODEX", output)
        self.assertIn("kaboom", output)

    def test_one_agent_crashing_does_not_affect_a_normal_return_for_another(self):
        # Not a shared/global failure path: each call is independent.
        class AlwaysFails:
            def run(self, on_status=None, on_wait=None):
                raise RuntimeError("always broken")

        class AlwaysWorks:
            def run(self, on_status=None, on_wait=None):
                return 0

        sleeps = []
        # Bound the flaky one to a single retry attempt via a sleeper that
        # raises after the first backoff, just to keep this test finite.
        def sleeper_then_stop(seconds):
            sleeps.append(seconds)
            raise SystemExit  # stand-in for "the thread would keep retrying forever in production"

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            keepalive_cli._supervised_run(AlwaysFails(), "codex", sleeper=sleeper_then_stop)
        self.assertEqual(sleeps, [keepalive_cli.RESTART_BACKOFF_SECONDS])

        # A separate, healthy agent is entirely unaffected.
        keepalive_cli._supervised_run(AlwaysWorks(), "claude", sleeper=lambda s: None)


class KeepaliveDisplayTests(unittest.TestCase):
    """The live table must refresh in place, never stack a fresh copy below
    the previous one during a normal wait."""

    def test_redraw_moves_cursor_up_instead_of_reprinting_below(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            printed = keepalive_cli._redraw("AGENT   LAST\nCLAUDE  10:00", 0)
        first_output = buf.getvalue()
        self.assertEqual(printed, 2)
        self.assertNotIn(f"{keepalive_cli.CSI}2A", first_output)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            printed2 = keepalive_cli._redraw("AGENT   LAST\nCLAUDE  10:05", printed)
        second_output = buf2.getvalue()
        self.assertIn(f"{keepalive_cli.CSI}2A", second_output)
        self.assertEqual(printed2, 2)

    def test_print_if_changed_suppresses_identical_repeats(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            last = keepalive_cli._print_if_changed("TABLE-A", None)
            last = keepalive_cli._print_if_changed("TABLE-A", last)
            last = keepalive_cli._print_if_changed("TABLE-A", last)
            last = keepalive_cli._print_if_changed("TABLE-B", last)
        output = buf.getvalue()
        self.assertEqual(output.count("TABLE-A"), 1)
        self.assertEqual(output.count("TABLE-B"), 1)

    def test_service_mode_never_calls_the_interactive_renderer(self):
        with patch.object(keepalive_core.KeepaliveAgent, "run", return_value=0), \
             patch.object(keepalive_cli, "_redraw") as redraw, \
             patch.object(keepalive_cli, "_print_if_changed") as print_if_changed, \
             patch.object(keepalive_cli.time, "sleep"), \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = keepalive_cli.main(["--service", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        redraw.assert_not_called()
        print_if_changed.assert_not_called()

    def test_service_mode_prints_no_ansi_escape_codes_at_all(self):
        with patch.object(keepalive_core.KeepaliveAgent, "run", return_value=0), \
             patch.object(keepalive_cli.time, "sleep"), \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            rc = keepalive_cli.main(["--service", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertNotIn(keepalive_cli.CSI, out.getvalue())
        self.assertNotIn(keepalive_cli.CSI, err.getvalue())

    def test_continuous_interactive_mode_still_uses_the_display_path(self):
        # Under the test runner stdout is not a tty, so the non-interactive
        # branch (_print_if_changed) is the one exercised here -- this is
        # still the ordinary continuous mode, not --service and not --once.
        # Deterministic by construction (no timing race): the fake run()
        # blocks until the display path has actually been called once.
        import threading as _threading
        released = _threading.Event()

        def fake_run(self, on_status=None, on_wait=None):
            released.wait(timeout=5)
            return 0

        def fake_print_if_changed(frame, last_frame):
            released.set()
            return frame

        with patch.object(keepalive_core.KeepaliveAgent, "run", fake_run), \
             patch.object(keepalive_cli, "_print_if_changed", side_effect=fake_print_if_changed) as print_if_changed, \
             patch.object(keepalive_cli.time, "sleep"), \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        print_if_changed.assert_called()


class KeepaliveIsolationTests(unittest.TestCase):
    """Hard architectural contract: keepalive has zero dependency on
    agentstatus.calibrator -- it must keep working if that package vanished.
    """

    def test_no_source_file_mentions_calibrator(self):
        offenders = []
        for path in KEEPALIVE_PACKAGE_ROOT.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "calibrator" in text.lower():
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"keepalive source references calibrator: {offenders}")

    def test_importing_keepalive_never_loads_calibrator_modules(self):
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from agentstatus import keepalive\n"
            "from agentstatus.keepalive import cli, core, persistence, providers\n"
            "assert not any(name == 'agentstatus.calibrator' or name.startswith('agentstatus.calibrator.') "
            "for name in sys.modules), sorted(sys.modules)\n"
            "print('OK')\n"
        ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)



class KeepaliveNoInterpretationTests(unittest.TestCase):
    """Keepalive must never classify, measure, or otherwise interpret a ping
    beyond its bare exit code -- checked structurally, not by scanning
    prose (this module's own docstrings legitimately name the very concepts
    it excludes, e.g. "no token/cache interpretation")."""

    def test_ping_result_carries_only_ok_and_error(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(PingResult)}
        self.assertEqual(names, {"ok", "error"})

    def test_ping_never_reads_anything_but_ok_and_error_from_the_result(self):
        source = inspect.getsource(keepalive_core.KeepaliveAgent.ping)
        self.assertIn("result.ok", source)
        self.assertIn("result.error", source)
        # No other attribute access on the ping outcome at all.
        self.assertNotRegex(source, r"result\.(?!ok\b|error\b)\w+")

    def test_no_classify_or_assess_method_exists_anywhere_in_keepalive(self):
        for module in (keepalive_core, keepalive_providers, keepalive_cli):
            for _name, obj in vars(module).items():
                if not inspect.isclass(obj):
                    continue
                self.assertFalse(hasattr(obj, "assess"), f"{obj} has an assess() method")
                self.assertFalse(hasattr(obj, "classify"), f"{obj} has a classify() method")


class KeepaliveSystemdUnitTests(unittest.TestCase):
    """The shipped systemd --user unit template and installer must be a
    non-privileged, self-restarting, user-level service definition."""

    SYSTEMD_DIR = KEEPALIVE_PACKAGE_ROOT / "systemd"
    UNIT_TEMPLATE = SYSTEMD_DIR / "agent-status-keepalive.service.in"
    INSTALL_SCRIPT = SYSTEMD_DIR / "install.sh"

    def test_unit_template_exists(self):
        self.assertTrue(self.UNIT_TEMPLATE.exists())

    def test_unit_runs_keepalive_in_service_mode(self):
        content = self.UNIT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("keepalive --service", content)

    def test_unit_has_no_root_or_sudo(self):
        content = self.UNIT_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("sudo", content.lower())
        self.assertNotRegex(content, r"(?im)^\s*User\s*=\s*root\s*$")

    def test_unit_restarts_on_failure_with_a_reasonable_delay(self):
        content = self.UNIT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("Restart=on-failure", content)
        match = None
        import re
        for line in content.splitlines():
            match = re.match(r"RestartSec=(\d+)", line.strip())
            if match:
                break
        self.assertIsNotNone(match, "no RestartSec= found")
        delay = int(match.group(1))
        self.assertGreater(delay, 0)
        self.assertLess(delay, 300)  # a "reasonable" delay, not near-instant crash-looping

    def test_unit_is_a_simple_type_not_a_forking_daemon(self):
        content = self.UNIT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("Type=simple", content)
        self.assertNotIn("Type=forking", content)

    def test_unit_targets_user_session_not_system(self):
        content = self.UNIT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("WantedBy=default.target", content)
        self.assertNotIn("multi-user.target", content)

    def test_install_script_exists_and_is_executable(self):
        self.assertTrue(self.INSTALL_SCRIPT.exists())
        self.assertTrue(os.access(self.INSTALL_SCRIPT, os.X_OK))

    def test_install_script_refuses_to_run_as_root(self):
        content = self.INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("id -u", content)

    def test_install_script_never_executes_systemctl_itself(self):
        # It may only ever *print* the systemctl commands for the user to
        # run -- never invoke systemctl (enable/start/daemon-reload) itself.
        content = self.INSTALL_SCRIPT.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if "systemctl" in stripped and not stripped.startswith("#"):
                self.assertTrue(stripped.startswith("echo"),
                                f"install.sh appears to execute systemctl directly: {line!r}")

    def test_install_script_writes_to_user_systemd_directory_not_system(self):
        content = self.INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(".config/systemd/user", content)
        self.assertNotIn("/etc/systemd", content)

    def test_install_script_resolves_provider_binaries_from_current_path(self):
        content = self.INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("command -v", content)


if __name__ == "__main__":
    unittest.main()
