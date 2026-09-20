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
from agentstatus.keepalive import history as keepalive_history_module
from agentstatus.keepalive import observe as keepalive_observe_module
from agentstatus.keepalive import providers as keepalive_providers
from agentstatus.keepalive.core import (
    ACTION_PING, ACTION_SKIP, ACTIVITY_WINDOW_SECONDS, AGENT_STAGGER_SECONDS,
    CYCLE_SECONDS, LEGACY_INTERVAL_SECONDS, CycleRunner, KeepaliveAgent,
    initial_state, iso, next_boundary,
)
from agentstatus.keepalive.persistence import STATE_SCHEMA, load_state, save_state
from agentstatus.keepalive.providers import ClaudeKeepalive, CodexKeepalive, GrokKeepalive, PingResult

UTC = dt.timezone.utc
KEEPALIVE_PACKAGE_ROOT = Path(keepalive.__file__).parent


class Clock:
    """A tiny, self-contained fake clock -- no dependency on any other
    test module in this project."""

    def __init__(self, start=None):
        self.value = start or dt.datetime(2026, 9, 20, 6, 0, tzinfo=UTC)

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
        self.activity_checks = 0

    def detect_activity(self):
        self.activity_checks += 1
        return self.activity_at

    def ping(self):
        self.pings += 1
        return next(self.results, PingResult(True, None))


class LegacyConstantTests(unittest.TestCase):
    """3001s is retained only as a documented historical constant."""

    def test_legacy_interval_is_pinned_at_3001_seconds_not_rounded_to_3000(self):
        self.assertEqual(LEGACY_INTERVAL_SECONDS, 3001)
        self.assertNotEqual(LEGACY_INTERVAL_SECONDS, 3000)

    def test_legacy_interval_is_not_read_anywhere_in_the_scheduling_path(self):
        # Structural: the scheduler classes/functions must not reference the
        # legacy constant at all (it may still be *defined* and documented).
        source = inspect.getsource(keepalive_core.next_boundary) \
            + inspect.getsource(keepalive_core.CycleRunner) \
            + inspect.getsource(keepalive_core.KeepaliveAgent)
        self.assertNotIn("LEGACY_INTERVAL_SECONDS", source)


class NextBoundaryTests(unittest.TestCase):
    def test_just_after_the_hour_rolls_to_the_half_hour(self):
        now = dt.datetime(2026, 9, 20, 14, 0, 1, tzinfo=UTC)
        self.assertEqual(next_boundary(now), dt.datetime(2026, 9, 20, 14, 30, tzinfo=UTC))

    def test_exactly_on_the_hour_rolls_to_the_half_hour(self):
        now = dt.datetime(2026, 9, 20, 14, 0, 0, tzinfo=UTC)
        self.assertEqual(next_boundary(now), dt.datetime(2026, 9, 20, 14, 30, tzinfo=UTC))

    def test_just_after_the_half_hour_rolls_to_the_next_hour(self):
        now = dt.datetime(2026, 9, 20, 14, 30, 1, tzinfo=UTC)
        self.assertEqual(next_boundary(now), dt.datetime(2026, 9, 20, 15, 0, tzinfo=UTC))

    def test_exactly_on_the_half_hour_rolls_to_the_next_hour(self):
        now = dt.datetime(2026, 9, 20, 14, 30, 0, tzinfo=UTC)
        self.assertEqual(next_boundary(now), dt.datetime(2026, 9, 20, 15, 0, tzinfo=UTC))

    def test_just_before_the_half_hour_still_rolls_to_the_half_hour(self):
        now = dt.datetime(2026, 9, 20, 14, 29, 59, tzinfo=UTC)
        self.assertEqual(next_boundary(now), dt.datetime(2026, 9, 20, 14, 30, tzinfo=UTC))

    def test_hour_rollover_across_midnight(self):
        now = dt.datetime(2026, 9, 20, 23, 45, tzinfo=UTC)
        self.assertEqual(next_boundary(now), dt.datetime(2026, 9, 21, 0, 0, tzinfo=UTC))

    def test_microseconds_are_discarded_not_rounded_up(self):
        now = dt.datetime(2026, 9, 20, 14, 29, 59, 999999, tzinfo=UTC)
        self.assertEqual(next_boundary(now), dt.datetime(2026, 9, 20, 14, 30, tzinfo=UTC))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(keepalive_core.KeepaliveError):
            next_boundary(dt.datetime(2026, 9, 20, 14, 0))

    def test_cycle_is_exactly_1800_seconds_apart_across_repeated_calls(self):
        # No cumulative drift: computed fresh from wall-clock time each
        # call, never additive from a previous boundary.
        first = next_boundary(dt.datetime(2026, 9, 20, 14, 0, 5, tzinfo=UTC))
        second = next_boundary(first)
        third = next_boundary(second)
        self.assertEqual((second - first).total_seconds(), CYCLE_SECONDS)
        self.assertEqual((third - second).total_seconds(), CYCLE_SECONDS)

    def test_a_cycle_that_finishes_late_does_not_delay_the_next_boundary(self):
        # Simulates a slow cycle: even if "now" has drifted well past the
        # scheduled boundary by the time we ask again, the next boundary is
        # still the real next :00/:30 on the wall clock -- not "boundary +
        # 1800s" relative to the missed one.
        boundary = dt.datetime(2026, 9, 20, 14, 30, tzinfo=UTC)
        late_now = boundary + dt.timedelta(seconds=1200)  # cycle overran by 20 minutes
        self.assertEqual(next_boundary(late_now), dt.datetime(2026, 9, 20, 15, 0, tzinfo=UTC))


class KeepaliveAgentDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, provider=None, state_name="agent.json"):
        return KeepaliveAgent(provider or FakeProvider(self.clock), self.root / state_name, clock=self.clock)

    def test_activity_window_is_1800_seconds_fixed(self):
        self.assertEqual(ACTIVITY_WINDOW_SECONDS, 1800)

    def test_no_known_activity_is_ping(self):
        agent = self.agent()
        self.assertEqual(agent.decide(self.clock(), None), ACTION_PING)

    def test_activity_exactly_at_the_window_edge_is_skip(self):
        agent = self.agent()
        now = self.clock()
        edge = now - dt.timedelta(seconds=ACTIVITY_WINDOW_SECONDS)
        self.assertEqual(agent.decide(now, edge), ACTION_SKIP)

    def test_activity_one_second_older_than_the_window_is_ping(self):
        agent = self.agent()
        now = self.clock()
        just_outside = now - dt.timedelta(seconds=ACTIVITY_WINDOW_SECONDS + 1)
        self.assertEqual(agent.decide(now, just_outside), ACTION_PING)

    def test_very_recent_activity_is_skip(self):
        agent = self.agent()
        now = self.clock()
        recent = now - dt.timedelta(seconds=30)
        self.assertEqual(agent.decide(now, recent), ACTION_SKIP)

    def test_activity_reported_in_the_future_is_still_ping_not_a_crash(self):
        # A clock-skewed/odd activity source must never be treated as
        # "recently active" via a negative age wrapping around.
        agent = self.agent()
        now = self.clock()
        future = now + dt.timedelta(seconds=60)
        self.assertEqual(agent.decide(now, future), ACTION_PING)


class KeepaliveAgentRunSlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, provider=None, state_name="agent.json"):
        return KeepaliveAgent(provider or FakeProvider(self.clock), self.root / state_name, clock=self.clock)

    def test_no_activity_pings_and_updates_state(self):
        provider = FakeProvider(self.clock, activity_at=None)
        state_path = self.root / "agent.json"
        agent = KeepaliveAgent(provider, state_path, clock=self.clock)
        record = agent.run_slot()
        self.assertEqual(record["action"], ACTION_PING)
        self.assertEqual(record["ping_status"], "ok")
        self.assertIsNone(record["ping_error"])
        self.assertIsNone(record["last_activity"])
        self.assertEqual(provider.pings, 1)
        persisted = load_state(state_path)
        self.assertEqual(persisted["last_status"], "ok")
        self.assertIsNotNone(persisted["last_ping_finished_at"])

    def test_recent_activity_skips_and_never_pings_or_touches_state(self):
        recent = self.clock() - dt.timedelta(seconds=10)
        provider = FakeProvider(self.clock, activity_at=recent)
        state_path = self.root / "agent.json"
        agent = KeepaliveAgent(provider, state_path, clock=self.clock)
        record = agent.run_slot()
        self.assertEqual(record["action"], ACTION_SKIP)
        self.assertIsNone(record["ping_status"])
        self.assertIsNone(record["ping_error"])
        self.assertEqual(record["last_activity"], iso(recent))
        self.assertEqual(provider.pings, 0, "a SKIP must never call provider.ping()")
        self.assertFalse(state_path.exists(), "a SKIP must never write scheduler state")

    def test_ping_failure_is_recorded_with_its_error(self):
        provider = FakeProvider(self.clock, results=[PingResult(False, "boom")])
        agent = self.agent(provider)
        record = agent.run_slot()
        self.assertEqual(record["action"], ACTION_PING)
        self.assertEqual(record["ping_status"], "fail")
        self.assertEqual(record["ping_error"], "boom")

    def test_every_slot_returns_a_record_ping_or_skip(self):
        for activity, expected in ((None, ACTION_PING), (self.clock(), ACTION_SKIP)):
            with self.subTest(activity=activity):
                provider = FakeProvider(self.clock, activity_at=activity)
                agent = self.agent(provider, state_name=f"agent-{expected}.json")
                record = agent.run_slot()
                self.assertEqual(record["action"], expected)
                self.assertIn("timestamp", record)
                self.assertIn("agent", record)


class KeepaliveAgentPingOnceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def test_ping_once_ignores_recent_activity_and_still_pings(self):
        recent = self.clock() - dt.timedelta(seconds=5)
        provider = FakeProvider(self.clock, activity_at=recent)
        agent = KeepaliveAgent(provider, self.root / "agent.json", clock=self.clock)
        record = agent.ping_once()
        self.assertEqual(record["action"], ACTION_PING)
        self.assertEqual(record["ping_status"], "ok")
        self.assertEqual(provider.pings, 1)

    def test_ping_once_never_calls_detect_activity_for_its_own_decision(self):
        # It may still be perfectly fine if detect_activity happens to be
        # called elsewhere, but ping_once's own decision must not depend on
        # it -- verified here by never even invoking it.
        provider = FakeProvider(self.clock)
        agent = KeepaliveAgent(provider, self.root / "agent.json", clock=self.clock)
        agent.ping_once()
        self.assertEqual(provider.activity_checks, 0)

    def test_ping_once_pings_exactly_once_and_updates_state_normally(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        agent = KeepaliveAgent(provider, state_path, clock=self.clock)
        statuses = []
        agent.ping_once(on_status=statuses.append)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(provider.pings, 1)
        persisted = load_state(state_path)
        self.assertEqual(persisted["last_status"], "ok")
        self.assertIsNotNone(persisted["last_ping_finished_at"])


class KeepaliveStatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def test_persisted_state_carries_no_token_cache_or_cost_fields(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        agent = KeepaliveAgent(provider, state_path, clock=self.clock)
        agent.run_slot()
        persisted = load_state(state_path)
        self.assertEqual(persisted["schema"], STATE_SCHEMA)
        self.assertEqual(set(persisted.keys()), {
            "schema", "agent", "last_ping_finished_at", "last_status", "last_error", "updated_at",
        })

    def test_identity_mismatch_on_resume_is_rejected(self):
        provider = FakeProvider(self.clock)
        state_path = self.root / "agent.json"
        save_state(state_path, {**initial_state(provider), "agent": "someone-else"})
        agent = KeepaliveAgent(provider, state_path, clock=self.clock)
        with self.assertRaises(Exception):
            agent.load()


class NamedFakeProvider(FakeProvider):
    """FakeProvider whose ``key`` matches the agent slot it stands in for --
    real provider classes (ClaudeKeepalive etc.) always satisfy this; the
    plain FakeProvider's fixed ``key = "test-agent"`` does not, which is
    exactly right for single-agent tests but wrong once multiple distinctly
    keyed agents run in the same cycle.
    """

    def __init__(self, key, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.key = key


class CycleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Start mid-cycle so the first boundary is deterministic and simple.
        self.clock = Clock(dt.datetime(2026, 9, 20, 14, 0, 1, tzinfo=UTC))

    def tearDown(self):
        self.temp.cleanup()

    def _agents(self, activity=None):
        providers = {key: NamedFakeProvider(key, self.clock, activity_at=activity)
                    for key in ("claude", "codex", "grok")}
        agents = [(key, KeepaliveAgent(providers[key], self.root / f"{key}.json", clock=self.clock))
                  for key in ("claude", "codex", "grok")]
        return providers, agents

    def runner(self, agents, **kwargs):
        return CycleRunner(agents, clock=self.clock, sleeper=self.clock.sleep, poll_interval=1.0, **kwargs)

    def test_fixed_agent_order_is_preserved(self):
        _providers, agents = self._agents()
        order_seen = []
        self.runner(agents).run(max_cycles=1, on_event=lambda r: order_seen.append(r["agent"]))
        self.assertEqual(order_seen, ["claude", "codex", "grok"])

    def test_stagger_is_exactly_5_seconds_between_agents(self):
        self.assertEqual(AGENT_STAGGER_SECONDS, 5)
        _providers, agents = self._agents()
        boundary = next_boundary(self.clock())
        timestamps = {}
        self.runner(agents).run(
            max_cycles=1,
            on_event=lambda r: timestamps.__setitem__(r["agent"], dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))),
        )
        self.assertEqual(timestamps["claude"], boundary)
        self.assertEqual(timestamps["codex"], boundary + dt.timedelta(seconds=5))
        self.assertEqual(timestamps["grok"], boundary + dt.timedelta(seconds=10))

    def test_stagger_is_a_floor_a_slow_slot_pushes_later_slots_but_never_earlier(self):
        # Confirmed in real production (2026-09-20): sequential execution
        # means the stagger is a minimum spacing, not a millisecond
        # guarantee -- a slot that genuinely takes longer than the stagger
        # itself (a real ping, or a slow provider-observation call in the
        # cli.py on_event callback) pushes every later slot in the same
        # cycle back by the same amount. It must never pull a later slot
        # earlier than its own boundary + offset, though.
        class SlowPingProvider(NamedFakeProvider):
            def ping(self):
                self.clock.sleep(20)  # simulates a slow real ping/observation
                return super().ping()

        clock = self.clock
        providers = {
            "claude": SlowPingProvider("claude", clock, activity_at=None),  # PING, slow
            "codex": NamedFakeProvider("codex", clock, activity_at=None),   # PING, fast
            "grok": NamedFakeProvider("grok", clock, activity_at=None),     # PING, fast
        }
        agents = [(key, KeepaliveAgent(providers[key], self.root / f"{key}.json", clock=clock))
                  for key in ("claude", "codex", "grok")]
        boundary = next_boundary(clock())
        timestamps = {}
        self.runner(agents).run(
            max_cycles=1,
            on_event=lambda r: timestamps.__setitem__(r["agent"], dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))),
        )
        self.assertEqual(timestamps["claude"], boundary + dt.timedelta(seconds=20))
        # codex's own nominal slot (boundary+5s) has already passed by the
        # time claude's slow ping finishes -- codex runs immediately after,
        # not earlier than its own nominal slot, and not exactly at +5s.
        self.assertEqual(timestamps["codex"], boundary + dt.timedelta(seconds=20))
        self.assertGreaterEqual(timestamps["codex"], boundary + dt.timedelta(seconds=AGENT_STAGGER_SECONDS))
        self.assertEqual(timestamps["grok"], boundary + dt.timedelta(seconds=20))
        self.assertGreaterEqual(timestamps["grok"], boundary + dt.timedelta(seconds=2 * AGENT_STAGGER_SECONDS))

    def test_slots_land_exactly_on_the_next_00_or_30(self):
        _providers, agents = self._agents()
        waits = []
        self.runner(agents).run(max_cycles=1, on_wait=waits.append)
        scheduled = dt.datetime.fromisoformat(waits[0]["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(scheduled, next_boundary(dt.datetime(2026, 9, 20, 14, 0, 1, tzinfo=UTC)))
        self.assertEqual(scheduled.minute, 30)
        self.assertEqual(scheduled.second, 0)

    def test_consecutive_cycles_do_not_cumulatively_drift(self):
        _providers, agents = self._agents()
        boundaries = []
        self.runner(agents).run(max_cycles=3, on_wait=lambda w: boundaries.append(
            dt.datetime.fromisoformat(w["scheduled_at"].replace("Z", "+00:00"))))
        self.assertEqual((boundaries[1] - boundaries[0]).total_seconds(), CYCLE_SECONDS)
        self.assertEqual((boundaries[2] - boundaries[1]).total_seconds(), CYCLE_SECONDS)

    def _always_active_agents(self):
        # A provider whose activity is always "30 seconds ago" relative to
        # whenever it is asked -- models a user continuously, actively
        # using that agent across multiple cycles (each cycle's own
        # activity check finds fresh evidence, not one static timestamp
        # that would eventually age out of the window on a later cycle).
        class AlwaysRecentProvider(NamedFakeProvider):
            def detect_activity(self):
                self.activity_checks += 1
                return self.clock() - dt.timedelta(seconds=30)

        providers = {key: AlwaysRecentProvider(key, self.clock) for key in ("claude", "codex", "grok")}
        agents = [(key, KeepaliveAgent(providers[key], self.root / f"{key}.json", clock=self.clock))
                  for key in ("claude", "codex", "grok")]
        return providers, agents

    def test_every_agent_gets_an_event_every_cycle_including_skip(self):
        _providers, agents = self._always_active_agents()
        events = []
        self.runner(agents).run(max_cycles=2, on_event=events.append)
        self.assertEqual(len(events), 6)  # 3 agents * 2 cycles
        self.assertTrue(all(e["action"] == ACTION_SKIP for e in events))

    def test_skip_never_calls_provider_ping(self):
        providers, agents = self._always_active_agents()
        self.runner(agents).run(max_cycles=1)
        self.assertEqual(sum(p.pings for p in providers.values()), 0)

    def test_no_activity_pings_every_agent_every_cycle(self):
        providers, agents = self._agents(activity=None)
        self.runner(agents).run(max_cycles=1)
        self.assertTrue(all(p.pings == 1 for p in providers.values()))

    def test_activity_isolation_one_agents_activity_never_affects_another(self):
        clock = self.clock
        claude_provider = NamedFakeProvider("claude", clock, activity_at=clock())  # recent -> skip
        codex_provider = NamedFakeProvider("codex", clock, activity_at=None)       # none -> ping
        grok_provider = NamedFakeProvider("grok", clock, activity_at=None)         # none -> ping
        agents = [
            ("claude", KeepaliveAgent(claude_provider, self.root / "claude.json", clock=clock)),
            ("codex", KeepaliveAgent(codex_provider, self.root / "codex.json", clock=clock)),
            ("grok", KeepaliveAgent(grok_provider, self.root / "grok.json", clock=clock)),
        ]
        events = {}
        self.runner(agents).run(max_cycles=1, on_event=lambda r: events.__setitem__(r["agent"], r))
        self.assertEqual(events["claude"]["action"], ACTION_SKIP)
        self.assertEqual(events["codex"]["action"], ACTION_PING)
        self.assertEqual(events["grok"]["action"], ACTION_PING)
        self.assertEqual(claude_provider.pings, 0)
        self.assertEqual(codex_provider.pings, 1)
        self.assertEqual(grok_provider.pings, 1)

    def test_one_agents_failure_does_not_prevent_the_others_slots(self):
        class ExplodingProvider(NamedFakeProvider):
            def detect_activity(self):
                raise RuntimeError("boom")

        exploding = ExplodingProvider("claude", self.clock)
        _codex_providers, agents = self._agents(activity=None)
        agents = [("claude", KeepaliveAgent(exploding, self.root / "claude.json", clock=self.clock))] + agents[1:]
        events = []
        self.runner(agents).run(max_cycles=1, on_event=events.append)
        self.assertEqual([e["agent"] for e in events], ["claude", "codex", "grok"])
        self.assertEqual(events[0]["action"], "error")
        self.assertIn("boom", events[0]["ping_error"])
        # codex and grok still ran their normal slot despite claude's crash.
        self.assertEqual(events[1]["action"], ACTION_PING)
        self.assertEqual(events[2]["action"], ACTION_PING)

    def test_one_agents_failure_does_not_delay_the_next_cycles_boundary(self):
        class ExplodingProvider(NamedFakeProvider):
            def detect_activity(self):
                raise RuntimeError("boom")

        exploding = ExplodingProvider("claude", self.clock)
        _p, agents = self._agents(activity=None)
        agents = [("claude", KeepaliveAgent(exploding, self.root / "claude.json", clock=self.clock))] + agents[1:]
        boundaries = []
        self.runner(agents).run(max_cycles=2, on_wait=lambda w: boundaries.append(
            dt.datetime.fromisoformat(w["scheduled_at"].replace("Z", "+00:00"))))
        self.assertEqual((boundaries[1] - boundaries[0]).total_seconds(), CYCLE_SECONDS)


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
        self.assertLess(elapsed, 5, "must return immediately, never wait for a cycle boundary")
        rendered = out.getvalue()
        self.assertIn("AGENT", rendered)
        self.assertIn("CLAUDE", rendered)
        self.assertIn("CODEX", rendered)
        self.assertIn("GROK", rendered)
        self.assertIn("ok", rendered)


class KeepaliveCliDispatchTests(unittest.TestCase):
    """--once and continuous/--service mode must route through different
    core mechanisms: a forced ping_once() vs. the real CycleRunner."""

    def test_once_calls_ping_once_never_the_cycle_runner(self):
        fixed_record = {
            "agent": "x", "action": ACTION_PING, "timestamp": iso(dt.datetime.now(UTC)),
            "last_activity": None, "ping_status": "ok", "ping_error": None,
        }
        with patch.object(keepalive_core.KeepaliveAgent, "ping_once", return_value=fixed_record) as ping_once, \
             patch.object(keepalive_core.CycleRunner, "run") as run, \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--once", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(ping_once.call_count, 3)
        run.assert_not_called()

    def test_continuous_mode_calls_cycle_runner_never_ping_once(self):
        with patch.object(keepalive_core.CycleRunner, "run", return_value=0) as run, \
             patch.object(keepalive_core.KeepaliveAgent, "ping_once") as ping_once, \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 1)
        ping_once.assert_not_called()

    def test_service_mode_calls_cycle_runner_via_supervised_wrapper(self):
        with patch.object(keepalive_core.CycleRunner, "run", return_value=0) as run, \
             patch.object(keepalive_core.KeepaliveAgent, "ping_once") as ping_once, \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = keepalive_cli.main(["--service", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 1)
        ping_once.assert_not_called()


class KeepaliveCycleNowTests(unittest.TestCase):
    """``--cycle-now``: exactly one normal, activity-aware cycle, run right
    now (this moment as the cycle's own boundary), through the same
    CycleRunner.run_cycle() and history/observation path the real :00/:30
    schedule uses -- never the forced/parallel ``--once`` path, never the
    normal continuous scheduler, and never altering the production cadence.
    """

    def test_cycle_now_calls_run_cycle_never_run_or_ping_once(self):
        fixed_records = [
            {"agent": key, "action": ACTION_SKIP, "timestamp": iso(dt.datetime.now(UTC)),
             "last_activity": iso(dt.datetime.now(UTC)), "ping_status": None, "ping_error": None}
            for key in ("claude", "codex", "grok")
        ]

        def fake_run_cycle(self, boundary, on_event=None):
            for record in fixed_records:
                if on_event:
                    on_event(record)
            return fixed_records

        with patch.object(keepalive_core.CycleRunner, "run_cycle", fake_run_cycle) as run_cycle, \
             patch.object(keepalive_core.CycleRunner, "run") as run, \
             patch.object(keepalive_core.KeepaliveAgent, "ping_once") as ping_once, \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--cycle-now", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        run.assert_not_called()
        ping_once.assert_not_called()

    def test_cycle_now_boundary_is_this_moment_not_the_next_00_or_30(self):
        captured = {}

        def fake_run_cycle(self, boundary, on_event=None):
            captured["boundary"] = boundary
            return []

        before = dt.datetime.now(UTC)
        with patch.object(keepalive_core.CycleRunner, "run_cycle", fake_run_cycle), \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--cycle-now", "--state-dir", tmp, "--model", "fixture"])
        after = dt.datetime.now(UTC)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(captured["boundary"], before)
        self.assertLessEqual(captured["boundary"], after)

    def test_cycle_now_exits_after_exactly_one_cycle(self):
        calls = {"n": 0}

        def fake_run_cycle(self, boundary, on_event=None):
            calls["n"] += 1
            return []

        with patch.object(keepalive_core.CycleRunner, "run_cycle", fake_run_cycle), \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()):
            rc = keepalive_cli.main(["--cycle-now", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 1)

    def _fake_providers(self, clock, activity):
        return {key: NamedFakeProvider(key, clock, activity_at=activity) for key in ("claude", "codex", "grok")}

    def _fake_cycle_runner_factory(self, clock):
        # main() constructs its own CycleRunner internally with the real
        # clock/sleeper (time.sleep, utc_now); without this, --cycle-now
        # would actually wait real wall-clock seconds for each staggered
        # slot (fine for a live smoke test, but far too slow -- and, worse,
        # would hang indefinitely whenever the fake `boundary` computed
        # from a patched utc_now() lands far from real wall-clock time).
        # Patching the name main() itself calls (keepalive_cli.CycleRunner)
        # with a factory that injects the same fake clock/sleeper mirrors
        # exactly how existing tests inject fake providers.
        def factory(agents):
            return CycleRunner(agents, clock=clock, sleeper=clock.sleep, poll_interval=1.0)
        return factory

    def _fake_keepalive_agent_factory(self, clock):
        # KeepaliveAgent instances are also constructed inside main() with
        # the real default clock; without this, a slot's own recorded
        # timestamp (self.clock() inside run_slot()) would still be the
        # real wall clock even while the CycleRunner's *wait* uses the fake
        # one above -- decoupling the two would make a staggered slot's
        # recorded timestamp not actually reflect the (fake) stagger.
        def factory(provider, state_path):
            return KeepaliveAgent(provider, state_path, clock=clock)
        return factory

    def test_cycle_now_skips_agents_with_recent_activity(self):
        clock = Clock()
        providers = self._fake_providers(clock, activity=clock())  # very recent
        fixed = _fixture_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with patch.object(keepalive_cli, "_build_provider", side_effect=lambda key, args: providers[key]), \
                 patch.object(keepalive_cli, "utc_now", clock), \
                 patch.object(keepalive_cli, "CycleRunner", side_effect=self._fake_cycle_runner_factory(clock)), \
                 patch.object(keepalive_cli, "KeepaliveAgent", side_effect=self._fake_keepalive_agent_factory(clock)), \
                 patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed), \
                 redirect_stdout(io.StringIO()):
                rc = keepalive_cli.main(["--cycle-now", "--state-dir", str(state_dir), "--model", "fixture"])
            self.assertEqual(rc, 0)
            for key in ("claude", "codex", "grok"):
                self.assertEqual(providers[key].pings, 0)
                events = keepalive_history_module.load_events(keepalive_history_module.history_path(state_dir, key))
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["action"], ACTION_SKIP)

    def test_cycle_now_pings_agents_with_no_recent_activity(self):
        clock = Clock()
        providers = self._fake_providers(clock, activity=None)
        fixed = _fixture_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with patch.object(keepalive_cli, "_build_provider", side_effect=lambda key, args: providers[key]), \
                 patch.object(keepalive_cli, "utc_now", clock), \
                 patch.object(keepalive_cli, "CycleRunner", side_effect=self._fake_cycle_runner_factory(clock)), \
                 patch.object(keepalive_cli, "KeepaliveAgent", side_effect=self._fake_keepalive_agent_factory(clock)), \
                 patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed), \
                 redirect_stdout(io.StringIO()):
                rc = keepalive_cli.main(["--cycle-now", "--state-dir", str(state_dir), "--model", "fixture"])
            self.assertEqual(rc, 0)
            for key in ("claude", "codex", "grok"):
                self.assertEqual(providers[key].pings, 1)
                events = keepalive_history_module.load_events(keepalive_history_module.history_path(state_dir, key))
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["action"], ACTION_PING)
                self.assertEqual(events[0]["ping_status"], "ok")

    def test_cycle_now_uses_fixed_order_and_0_5_10_second_stagger(self):
        # Verifies the real stagger timing end-to-end through main() -- not
        # just at the CycleRunner level (already covered separately in
        # CycleRunnerTests) -- without waiting 10 real seconds, via the same
        # fake-clock CycleRunner injection as the tests above.
        clock = Clock()
        providers = self._fake_providers(clock, activity=None)
        fixed = _fixture_snapshot()

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with patch.object(keepalive_cli, "_build_provider", side_effect=lambda key, args: providers[key]), \
                 patch.object(keepalive_cli, "utc_now", clock), \
                 patch.object(keepalive_cli, "CycleRunner", side_effect=self._fake_cycle_runner_factory(clock)), \
                 patch.object(keepalive_cli, "KeepaliveAgent", side_effect=self._fake_keepalive_agent_factory(clock)), \
                 patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed), \
                 redirect_stdout(io.StringIO()):
                rc = keepalive_cli.main(["--cycle-now", "--state-dir", str(state_dir), "--model", "fixture"])
            self.assertEqual(rc, 0)
            timestamps = {}
            for key in ("claude", "codex", "grok"):
                events = keepalive_history_module.load_events(keepalive_history_module.history_path(state_dir, key))
                timestamps[key] = dt.datetime.fromisoformat(events[0]["timestamp"].replace("Z", "+00:00"))
        self.assertEqual((timestamps["codex"] - timestamps["claude"]).total_seconds(), AGENT_STAGGER_SECONDS)
        self.assertEqual((timestamps["grok"] - timestamps["claude"]).total_seconds(), 2 * AGENT_STAGGER_SECONDS)

    def test_cycle_now_one_agents_failure_does_not_block_the_others(self):
        clock = Clock()

        class ExplodingProvider(NamedFakeProvider):
            def ping(self):
                raise RuntimeError("boom")

        providers = {
            "claude": NamedFakeProvider("claude", clock, activity_at=None),
            "codex": ExplodingProvider("codex", clock, activity_at=None),
            "grok": NamedFakeProvider("grok", clock, activity_at=None),
        }
        fixed = _fixture_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with patch.object(keepalive_cli, "_build_provider", side_effect=lambda key, args: providers[key]), \
                 patch.object(keepalive_cli, "utc_now", clock), \
                 patch.object(keepalive_cli, "CycleRunner", side_effect=self._fake_cycle_runner_factory(clock)), \
                 patch.object(keepalive_cli, "KeepaliveAgent", side_effect=self._fake_keepalive_agent_factory(clock)), \
                 patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed), \
                 redirect_stdout(io.StringIO()):
                rc = keepalive_cli.main(["--cycle-now", "--state-dir", str(state_dir), "--model", "fixture"])
            self.assertEqual(rc, 0)
            claude_events = keepalive_history_module.load_events(keepalive_history_module.history_path(state_dir, "claude"))
            codex_events = keepalive_history_module.load_events(keepalive_history_module.history_path(state_dir, "codex"))
            grok_events = keepalive_history_module.load_events(keepalive_history_module.history_path(state_dir, "grok"))
        self.assertEqual(claude_events[0]["action"], ACTION_PING)
        self.assertEqual(codex_events[0]["action"], "error")
        self.assertIn("boom", codex_events[0]["ping_error"])
        self.assertEqual(grok_events[0]["action"], ACTION_PING)  # grok's own slot still ran

    def test_cycle_now_does_not_change_the_next_00_30_boundary_computation(self):
        # Structural: --cycle-now must never call next_boundary() at all --
        # it is entirely orthogonal to the normal production schedule.
        source = inspect.getsource(keepalive_cli.main)
        cycle_now_branch = source.split("if args.cycle_now:")[1].split("if args.service:")[0]
        self.assertNotIn("next_boundary", cycle_now_branch)

    def test_once_is_unaffected_by_cycle_now_existing(self):
        # Regression: --once must still be the forced, parallel, no-activity-
        # check ping_once() path, completely unchanged.
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
        self.assertLess(elapsed, 5, "--once must still return immediately, unaffected by --cycle-now")


def _fixture_snapshot():
    from agentstatus.keepalive import observe
    return keepalive_observe_module.Snapshot("unavailable", None, None, "fixture")


class KeepaliveSupervisedRunTests(unittest.TestCase):
    """A scheduler-loop failure must not silently end the process -- only a
    normal per-agent slot failure (already caught inside CycleRunner.run_cycle)
    reaches this; this covers _supervised_run's handling of anything
    unexpected escaping CycleRunner.run() itself."""

    def test_survives_an_unexpected_exception_and_retries_after_backoff(self):
        calls = {"n": 0}

        class FlakyRunner:
            def run(self, on_event=None, on_wait=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")
                return 0  # succeeds on retry

        sleeps = []
        with redirect_stderr(io.StringIO()):
            keepalive_cli._supervised_run(FlakyRunner(), sleeper=sleeps.append)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sleeps, [keepalive_cli.RESTART_BACKOFF_SECONDS])

    def test_logs_the_exception_to_stderr(self):
        class FlakyRunner:
            def __init__(self):
                self.calls = 0

            def run(self, on_event=None, on_wait=None):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("kaboom")
                return 0

        buf = io.StringIO()
        with redirect_stderr(buf):
            keepalive_cli._supervised_run(FlakyRunner(), sleeper=lambda seconds: None)
        output = buf.getvalue()
        self.assertIn("kaboom", output)


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
        with patch.object(keepalive_core.CycleRunner, "run", return_value=0), \
             patch.object(keepalive_cli, "_redraw") as redraw, \
             patch.object(keepalive_cli, "_print_if_changed") as print_if_changed, \
             tempfile.TemporaryDirectory() as tmp, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = keepalive_cli.main(["--service", "--state-dir", tmp, "--model", "fixture"])
        self.assertEqual(rc, 0)
        redraw.assert_not_called()
        print_if_changed.assert_not_called()

    def test_service_mode_prints_no_ansi_escape_codes_at_all(self):
        with patch.object(keepalive_core.CycleRunner, "run", return_value=0), \
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

        def fake_run(self, on_event=None, on_wait=None):
            released.wait(timeout=5)
            return 0

        def fake_print_if_changed(frame, last_frame):
            released.set()
            return frame

        with patch.object(keepalive_core.CycleRunner, "run", fake_run), \
             patch.object(keepalive_cli, "_print_if_changed", side_effect=fake_print_if_changed) as print_if_changed, \
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
    """Keepalive must never classify or interpret a *ping's own output*
    beyond its bare exit code -- checked structurally, not by scanning
    prose. The one documented exception is the PING/SKIP activity-window
    decision itself, which is the whole point of this module now."""

    def test_ping_result_carries_only_ok_and_error(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(PingResult)}
        self.assertEqual(names, {"ok", "error"})

    def test_do_ping_never_reads_anything_but_ok_and_error_from_the_result(self):
        source = inspect.getsource(keepalive_core.KeepaliveAgent._do_ping)
        self.assertIn("result.ok", source)
        self.assertIn("result.error", source)
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
