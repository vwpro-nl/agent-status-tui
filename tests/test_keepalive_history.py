"""Keepalive chronological event history: persistence, retention,
provider-native observation, rendering, and the ``history``/``monitor`` CLI
subcommands. Deliberately self-contained (no dependency on the removed
calibrator package): nothing here imports anything calibrator-related.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentstatus.keepalive import cli as keepalive_cli
from agentstatus.keepalive import history
from agentstatus.keepalive import observe
from agentstatus.keepalive import render
from agentstatus.keepalive.providers import ClaudeKeepalive, CodexKeepalive, GrokKeepalive, PingResult

UTC = dt.timezone.utc


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def make_event(agent: str, timestamp: dt.datetime, *, action: str = "ping", ping_status: str = "ok",
               ping_error=None, last_activity=None, five_hour=None, weekly=None,
               observation_status: str = "ok", observation_detail=None) -> dict:
    return {
        "timestamp": iso(timestamp), "agent": agent, "action": action,
        "last_activity": iso(last_activity) if isinstance(last_activity, dt.datetime) else last_activity,
        "ping_status": ping_status if action == "ping" else None,
        "ping_error": ping_error, "five_hour": five_hour, "weekly": weekly,
        "observation_status": observation_status, "observation_detail": observation_detail,
    }


class HistoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_loads_as_empty(self):
        self.assertEqual(history.load_events(self.root / "codex.jsonl"), [])

    def test_record_and_load_roundtrip(self):
        now = dt.datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
        path = self.root / "codex.jsonl"
        event = make_event("codex", now, five_hour={"used_percent": 34, "resets_at": iso(now)})
        history.record_event(path, event, now=now)
        events = history.load_events(path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent"], "codex")
        self.assertEqual(events[0]["schema"], history.HISTORY_SCHEMA)
        self.assertEqual(events[0]["five_hour"]["used_percent"], 34)

    def test_multiple_events_accumulate_in_one_file(self):
        base = dt.datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
        path = self.root / "claude.jsonl"
        for i in range(5):
            when = base + dt.timedelta(minutes=i)
            history.record_event(path, make_event("claude", when), now=when)
        self.assertEqual(len(history.load_events(path)), 5)

    def test_chronological_sort_across_agents(self):
        base = dt.datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
        codex_path, claude_path, grok_path = (self.root / f"{a}.jsonl" for a in ("codex", "claude", "grok"))
        # Recorded out of chronological order on purpose.
        history.record_event(claude_path, make_event("claude", base + dt.timedelta(minutes=18)), now=base)
        history.record_event(codex_path, make_event("codex", base + dt.timedelta(minutes=0)), now=base)
        history.record_event(grok_path, make_event("grok", base + dt.timedelta(minutes=30)), now=base)
        history.record_event(codex_path, make_event("codex", base + dt.timedelta(minutes=33)), now=base)

        merged = history.merge_chronological([codex_path, claude_path, grok_path])
        agents_in_order = [e["agent"] for e in merged]
        self.assertEqual(agents_in_order, ["codex", "claude", "grok", "codex"])
        timestamps = [e["timestamp"] for e in merged]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_provider_isolation_writing_one_agent_never_touches_another(self):
        now = dt.datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
        codex_path = self.root / "codex.jsonl"
        claude_path = self.root / "claude.jsonl"
        grok_path = self.root / "grok.jsonl"
        history.record_event(codex_path, make_event("codex", now), now=now)
        self.assertFalse(claude_path.exists())
        self.assertFalse(grok_path.exists())
        self.assertEqual(len(history.load_events(codex_path)), 1)

    def test_retention_prunes_events_older_than_16_days(self):
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        very_old = now - dt.timedelta(days=30)
        path = self.root / "codex.jsonl"
        history.record_event(path, make_event("codex", very_old), now=very_old)
        history.record_event(path, make_event("codex", now), now=now)
        events = history.load_events(path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["timestamp"], iso(now))

    def test_retention_boundary_is_exactly_16_days_inclusive(self):
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        exactly_16_days_old = now - dt.timedelta(days=history.RETENTION_DAYS)
        one_second_older_than_that = exactly_16_days_old - dt.timedelta(seconds=1)
        path = self.root / "codex.jsonl"
        history.record_event(path, make_event("codex", one_second_older_than_that),
                             now=one_second_older_than_that)
        history.record_event(path, make_event("codex", exactly_16_days_old), now=exactly_16_days_old)
        # This final write's own `now` triggers the retention pass.
        history.record_event(path, make_event("codex", now), now=now)
        timestamps = {e["timestamp"] for e in history.load_events(path)}
        self.assertIn(iso(exactly_16_days_old), timestamps, "exactly-16-days-old event must be kept")
        self.assertNotIn(iso(one_second_older_than_that), timestamps,
                         "an event one second past the 16-day boundary must be pruned")
        self.assertIn(iso(now), timestamps)

    def test_prune_is_a_pure_function_independent_of_record_event(self):
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        events = [
            make_event("codex", now - dt.timedelta(days=20)),
            make_event("codex", now - dt.timedelta(days=1)),
            make_event("codex", now),
        ]
        kept = history.prune(events, now)
        self.assertEqual(len(kept), 2)

    def test_prune_keeps_events_with_unparseable_timestamps_rather_than_dropping_them(self):
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        malformed = {"schema": history.HISTORY_SCHEMA, "agent": "codex", "timestamp": "not-a-timestamp"}
        kept = history.prune([malformed], now)
        self.assertEqual(kept, [malformed])

    def test_corrupt_line_is_skipped_not_raised(self):
        path = self.root / "codex.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        good = make_event("codex", now)
        path.write_text(
            "not json at all\n"
            + '{"schema":"wrong-schema/v1","agent":"codex"}\n'
            + '{"incomplete": \n'
            + __import__("json").dumps({"schema": history.HISTORY_SCHEMA, **good}) + "\n"
        )
        events, skipped = history.load_events_with_stats(path)
        self.assertEqual(len(events), 1)
        self.assertEqual(skipped, 3)

    def test_corrupt_history_does_not_block_a_new_event_from_being_recorded(self):
        path = self.root / "codex.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not valid json\n{also not valid\n")
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        # Must not raise despite the file being pre-existing garbage.
        history.record_event(path, make_event("codex", now), now=now)
        events = history.load_events(path)
        self.assertEqual(len(events), 1)

    def test_corrupt_history_self_heals_after_next_record(self):
        path = self.root / "codex.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage line one\ngarbage line two\n")
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        history.record_event(path, make_event("codex", now), now=now)
        # The rewrite drops the unrecoverable garbage entirely.
        raw = path.read_text()
        self.assertNotIn("garbage", raw)

    def test_history_file_is_written_with_restrictive_permissions(self):
        path = self.root / "codex.jsonl"
        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        history.record_event(path, make_event("codex", now), now=now)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)


class ObserveClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "claude-home"
        self.home.mkdir()
        self._env_backup = {
            k: os.environ.get(k) for k in ("AGENT_STATUS_CLAUDE_JSON", "AGENT_STATUS_OMARCHY_CACHE")
        }

    def tearDown(self):
        self.temp.cleanup()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_no_cache_anywhere_is_unavailable(self):
        os.environ["AGENT_STATUS_CLAUDE_JSON"] = str(Path(self.temp.name) / "nope.json")
        os.environ["AGENT_STATUS_OMARCHY_CACHE"] = str(Path(self.temp.name) / "nope2.json")
        snapshot = observe.observe_claude(self.home)
        self.assertEqual(snapshot.status, "unavailable")
        self.assertIsNone(snapshot.five_hour)
        self.assertIsNone(snapshot.weekly)
        self.assertIsNotNone(snapshot.detail)

    def test_omarchy_cache_is_read_when_present(self):
        import json
        omarchy_path = Path(self.temp.name) / "omarchy.json"
        omarchy_path.write_text(json.dumps({
            "fetchedAtMs": 1_800_000_000_000,
            "limits": [
                {"label": "5-hour", "percent": 0.34, "resetsAt": "2026-09-20T04:00:00Z"},
                {"label": "7-day", "percent": 0.46, "resetsAt": "2026-09-21T00:00:00Z"},
            ],
        }))
        os.environ["AGENT_STATUS_OMARCHY_CACHE"] = str(omarchy_path)
        os.environ["AGENT_STATUS_CLAUDE_JSON"] = str(Path(self.temp.name) / "nope.json")
        snapshot = observe.observe_claude(self.home)
        self.assertEqual(snapshot.status, "ok")
        self.assertAlmostEqual(snapshot.five_hour.used_percent, 34.0)
        self.assertAlmostEqual(snapshot.weekly.used_percent, 46.0)
        self.assertEqual(snapshot.five_hour.resets_at, "2026-09-20T04:00:00Z")

    def test_falls_back_to_claude_json_cached_usage_utilization(self):
        import json
        claude_json_path = Path(self.temp.name) / "claude.json"
        claude_json_path.write_text(json.dumps({
            "cachedUsageUtilization": {
                "fetchedAtMs": 1_800_000_000_000,
                "utilization": {
                    "five_hour": {"utilization": 63, "resets_at": "2026-09-20T04:18:00Z"},
                    "seven_day": {"utilization": 62, "resets_at": "2026-09-22T09:40:00Z"},
                },
            },
        }))
        os.environ["AGENT_STATUS_OMARCHY_CACHE"] = str(Path(self.temp.name) / "no-omarchy.json")
        os.environ["AGENT_STATUS_CLAUDE_JSON"] = str(claude_json_path)
        snapshot = observe.observe_claude(self.home)
        self.assertEqual(snapshot.status, "ok")
        self.assertAlmostEqual(snapshot.five_hour.used_percent, 63.0)
        self.assertAlmostEqual(snapshot.weekly.used_percent, 62.0)

    def test_never_opens_claude_credentials_file(self):
        # AST-based, not a raw text scan: the module's own docstring
        # legitimately *mentions* that this file is never opened -- what
        # must never exist is a call argument actually naming it.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(observe))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and "credentials" in arg.value):
                    self.fail(f"credentials path used as a call argument: {arg.value!r}")


class ObserveGrokTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_auth_file_is_unavailable(self):
        snapshot = observe.observe_grok(self.home)
        self.assertEqual(snapshot.status, "unavailable")
        self.assertIn("credential", snapshot.detail)

    def test_only_refresh_token_present_is_unavailable(self):
        import json
        (self.home / "auth.json").write_text(json.dumps({
            "default": {"refresh_token": "should-never-be-used"},
        }))
        snapshot = observe.observe_grok(self.home)
        self.assertEqual(snapshot.status, "unavailable")

    def test_expired_key_is_not_used(self):
        import json
        (self.home / "auth.json").write_text(json.dumps({
            "default": {"key": "expired-token", "expires_at": "2000-01-01T00:00:00Z"},
        }))
        snapshot = observe.observe_grok(self.home)
        self.assertEqual(snapshot.status, "unavailable")

    def test_valid_token_reaches_billing_request_and_maps_weekly_window(self):
        import json
        future = (dt.datetime.now(UTC) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        (self.home / "auth.json").write_text(json.dumps({
            "default": {"key": "live-token", "expires_at": future},
        }))
        seen = {}

        def fake_billing_request(token):
            seen["token"] = token
            return {
                "config": {
                    "creditUsagePercent": 12,
                    "currentPeriod": {
                        "type": observe.GROK_WEEKLY_PERIOD,
                        "start": "2026-09-15T00:00:00Z",
                        "end": "2026-09-22T00:00:00Z",
                    },
                },
            }

        with patch.object(observe, "_grok_billing_request", side_effect=fake_billing_request):
            snapshot = observe.observe_grok(self.home)
        self.assertEqual(seen["token"], "live-token")
        self.assertEqual(snapshot.status, "ok")
        self.assertIsNone(snapshot.five_hour, "Grok has no native 5h source; must never be fabricated")
        self.assertAlmostEqual(snapshot.weekly.used_percent, 12.0)
        self.assertEqual(snapshot.weekly.resets_at, "2026-09-22T00:00:00Z")

    def test_weekly_period_present_but_null_percent_keeps_the_reset(self):
        # Live-observed shape (2026-09-20): currentPeriod.type is correctly
        # weekly with real start/end bounds, but creditUsagePercent itself
        # came back null from Grok's billing endpoint. Percentage and reset
        # are independent facts: the official Grok TUI renders a missing
        # percent as 0% (a client-side fallback, not a proven server
        # value), which this project must not copy -- used_percent stays
        # unknown (None, rendered "--"), while the genuinely reported reset
        # is still used and shown, not thrown away along with the percent.
        import json
        future = (dt.datetime.now(UTC) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        (self.home / "auth.json").write_text(json.dumps({
            "default": {"key": "live-token", "expires_at": future},
        }))

        def fake_billing_request(token):
            return {
                "config": {
                    "creditUsagePercent": None,
                    "currentPeriod": {
                        "type": observe.GROK_WEEKLY_PERIOD,
                        "start": "2026-09-19T16:26:02Z", "end": "2026-09-26T16:26:02Z",
                    },
                },
            }

        with patch.object(observe, "_grok_billing_request", side_effect=fake_billing_request):
            snapshot = observe.observe_grok(self.home)
        self.assertEqual(snapshot.status, "ok")
        self.assertIsNotNone(snapshot.weekly)
        self.assertIsNone(snapshot.weekly.used_percent)          # never fabricated as 0%
        self.assertEqual(snapshot.weekly.resets_at, "2026-09-26T16:26:02Z")  # reset kept

    def test_weekly_period_present_percent_and_reset_both_missing_is_unavailable(self):
        import json
        future = (dt.datetime.now(UTC) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        (self.home / "auth.json").write_text(json.dumps({
            "default": {"key": "live-token", "expires_at": future},
        }))

        def fake_billing_request(token):
            return {"config": {"creditUsagePercent": None,
                               "currentPeriod": {"type": observe.GROK_WEEKLY_PERIOD}}}

        with patch.object(observe, "_grok_billing_request", side_effect=fake_billing_request):
            snapshot = observe.observe_grok(self.home)
        self.assertEqual(snapshot.status, "unavailable")
        self.assertIsNone(snapshot.weekly)

    def test_non_weekly_period_gives_a_different_reason(self):
        import json
        future = (dt.datetime.now(UTC) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        (self.home / "auth.json").write_text(json.dumps({
            "default": {"key": "live-token", "expires_at": future},
        }))

        def fake_billing_request(token):
            return {"config": {"creditUsagePercent": 5, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_DAILY"}}}

        with patch.object(observe, "_grok_billing_request", side_effect=fake_billing_request):
            snapshot = observe.observe_grok(self.home)
        self.assertEqual(snapshot.status, "unavailable")
        self.assertIn("not weekly", snapshot.detail)

    def test_billing_request_failure_is_unavailable_not_raised(self):
        import json
        future = (dt.datetime.now(UTC) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        (self.home / "auth.json").write_text(json.dumps({
            "default": {"key": "live-token", "expires_at": future},
        }))
        with patch.object(observe, "_grok_billing_request", side_effect=OSError("network down")):
            snapshot = observe.observe_grok(self.home)
        self.assertEqual(snapshot.status, "unavailable")
        self.assertNotIn("network down", snapshot.detail or "")

    def test_never_reads_refresh_token_field_structurally(self):
        import inspect
        source = inspect.getsource(observe)
        self.assertNotIn('"refresh_token"', source)
        self.assertNotIn("'refresh_token'", source)


class ObserveCodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_executable_is_unavailable(self):
        snapshot = observe.observe_codex(self.home, codex_command=["definitely-not-a-real-codex-binary-xyz"])
        self.assertEqual(snapshot.status, "unavailable")
        self.assertIn("not found", snapshot.detail)

    def test_parse_rate_limits_maps_primary_and_secondary(self):
        response = {
            "id": 2,
            "result": {
                "rateLimits": {
                    "primary": {"usedPercent": 34, "resetsAt": 1_800_000_000},
                    "secondary": {"usedPercent": 46, "resetsAt": 1_800_100_000},
                },
            },
        }
        five_hour, weekly = observe._parse_codex_rate_limits(response)
        self.assertEqual(five_hour.used_percent, 34)
        self.assertEqual(weekly.used_percent, 46)
        self.assertTrue(five_hour.resets_at.endswith("Z"))

    def test_parse_rate_limits_by_limit_id_shape(self):
        response = {
            "result": {
                "rateLimitsByLimitId": {
                    "codex": {"primary": {"usedPercent": 10}, "secondary": {"usedPercent": 20}},
                },
            },
        }
        five_hour, weekly = observe._parse_codex_rate_limits(response)
        self.assertEqual(five_hour.used_percent, 10)
        self.assertEqual(weekly.used_percent, 20)

    def test_malformed_response_yields_none(self):
        self.assertIsNone(observe._parse_codex_rate_limits({"nonsense": True}))
        self.assertIsNone(observe._parse_codex_rate_limits("not even a dict"))

    def test_end_to_end_app_server_json_rpc_round_trip(self):
        # A minimal fake app-server that speaks just enough of the protocol
        # observe_codex() actually uses: it never starts a real Codex CLI,
        # never sends a prompt, and never starts a model turn.
        script = self.home / "fake_app_server.py"
        script.write_text(textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("id") == 1 and msg.get("method") == "initialize":
                    sys.stdout.write(json.dumps({"id": 1, "result": {}}) + "\\n")
                    sys.stdout.flush()
                elif msg.get("id") == 2 and msg.get("method") == "account/rateLimits/read":
                    sys.stdout.write(json.dumps({
                        "id": 2,
                        "result": {"rateLimits": {
                            "primary": {"usedPercent": 34, "resetsAt": 1_800_000_000},
                            "secondary": {"usedPercent": 46, "resetsAt": 1_800_100_000},
                        }},
                    }) + "\\n")
                    sys.stdout.flush()
                    break
        """))
        snapshot = observe.observe_codex(self.home, codex_command=[sys.executable, str(script)])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.five_hour.used_percent, 34)
        self.assertEqual(snapshot.weekly.used_percent, 46)

    def test_never_starts_a_codex_exec_model_turn(self):
        import inspect
        source = inspect.getsource(observe.observe_codex)
        self.assertNotIn('"exec"', source)
        self.assertNotIn("'exec'", source)


class ObserveDispatchTests(unittest.TestCase):
    def test_dispatch_never_raises_even_on_internal_bug(self):
        with patch.object(observe, "observe_codex", side_effect=RuntimeError("boom")):
            snapshot = observe.observe("codex", codex_home=Path("/tmp"), codex_command=["codex"])
        self.assertEqual(snapshot.status, "unavailable")

    def test_unknown_agent_key_is_unavailable_not_a_crash(self):
        snapshot = observe.observe("not-a-real-agent")
        self.assertEqual(snapshot.status, "unavailable")


class RenderDisplayContractTests(unittest.TestCase):
    """The agreed column headers and PING/SKIP presentation, verbatim."""

    def test_combined_header_matches_the_agreed_contract(self):
        header = render.render_header(show_agent=True)
        # Column order and presence, not exact whitespace (widths are an
        # implementation detail) -- but every labelled column must appear,
        # in this order, with AGENT included.
        labels = [w for w in header.split("  ") if w.strip()]
        self.assertEqual([l.strip() for l in labels],
                         ["TIME", "AGENT", "ACTION", "LAST ACTIVITY", "PING", "5H", "RESET", "WEEK", "RESET"])

    def test_per_agent_header_matches_the_agreed_contract_without_agent(self):
        header = render.render_header(show_agent=False)
        labels = [w for w in header.split("  ") if w.strip()]
        self.assertEqual([l.strip() for l in labels],
                         ["TIME", "ACTION", "LAST ACTIVITY", "PING", "5H", "RESET", "WEEK", "RESET"])

    def test_skip_event_shows_action_skip_and_ping_dashdash(self):
        activity = dt.datetime(2026, 9, 20, 11, 45, tzinfo=UTC)
        event = make_event("claude", dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC), action="skip",
                           last_activity=activity)
        line = render.render_event(event)
        self.assertIn("SKIP", line)
        # PING must show exactly "--" for a SKIP, never OK/FAILED, and the
        # row must carry the real (locally-rendered) last-activity time,
        # not "--", since it was known.
        expected_activity_text = render.format_local_time(iso(activity))
        self.assertIn(expected_activity_text, line)
        self.assertRegex(line, rf"SKIP\s+{expected_activity_text}\s+--")

    def test_ping_ok_event_shows_action_ping_and_ping_ok(self):
        event = make_event("claude", dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC), action="ping", ping_status="ok")
        line = render.render_event(event)
        self.assertRegex(line, r"PING\s+--\s+OK\b")

    def test_ping_failed_event_shows_action_ping_and_ping_failed(self):
        event = make_event("grok", dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC), action="ping",
                           ping_status="fail", ping_error="exit 1")
        line = render.render_event(event)
        self.assertRegex(line, r"PING\s+--\s+FAILED\b")

    def test_skip_with_unknown_activity_shows_dashdash_for_last_activity(self):
        event = make_event("claude", dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC), action="skip",
                           last_activity=None)
        line = render.render_event(event)
        self.assertRegex(line, r"SKIP\s+--\s+--")


class RenderTests(unittest.TestCase):
    def test_missing_quota_and_reset_render_as_dashdash(self):
        self.assertEqual(render.format_percent(None), "--")
        self.assertEqual(render.format_reset(None, "2026-09-20T00:00:00Z"), "--")
        self.assertEqual(render.format_reset("2026-09-20T00:00:00Z", None), "--")

    def test_known_percent_and_future_reset(self):
        self.assertEqual(render.format_percent(34), "34%")
        self.assertEqual(render.format_reset("2026-09-20T00:29:00Z", "2026-09-20T00:00:00Z"), "29m")

    def test_reset_already_passed_is_now(self):
        self.assertEqual(render.format_reset("2026-09-20T00:00:00Z", "2026-09-20T00:05:00Z"), "now")

    def test_provider_without_5h_source_renders_dashdash(self):
        event = make_event("grok", dt.datetime(2026, 9, 20, tzinfo=UTC),
                           five_hour=None, weekly={"used_percent": 12, "resets_at": None})
        line = render.render_event(event)
        columns = [c.strip() for c in line.split("  ") if c.strip()]
        self.assertIn("--", line)
        self.assertNotIn("0%", line)  # never a fabricated zero

    def test_ping_ok_never_renders_as_delivered_anywhere(self):
        event = make_event("codex", dt.datetime(2026, 9, 20, tzinfo=UTC), ping_status="ok",
                           observation_status="unavailable", observation_detail="no live data")
        line = render.render_event(event)
        self.assertIn("OK", line)
        self.assertNotIn("DELIVERED", line.upper().replace("PROVIDER STATUS", ""))
        # A ping that succeeded technically, with no provider evidence at
        # all, must show honest dashes -- never a fabricated quota value.
        self.assertIn("--", line)

    def test_ping_failure_shows_fail_and_the_error_text(self):
        event = make_event("grok", dt.datetime(2026, 9, 20, tzinfo=UTC), ping_status="fail",
                           ping_error="grok exited 1: 402 quota exhausted")
        line = render.render_event(event)
        self.assertIn("FAIL", line)
        self.assertIn("402 quota exhausted", line)

    def test_render_events_is_chronological_all_agents_interleaved_not_grouped(self):
        base = dt.datetime(2026, 9, 20, tzinfo=UTC)
        events = [
            make_event("codex", base + dt.timedelta(minutes=33)),
            make_event("claude", base + dt.timedelta(minutes=18)),
            make_event("grok", base + dt.timedelta(minutes=30)),
        ]
        rendered = render.render_events(events)
        # Rendered in the order given (callers are responsible for sorting
        # via history.merge_chronological before calling render_events).
        lines = [l for l in rendered.splitlines() if not l.strip().startswith(("provider status", "ping error"))]
        self.assertEqual(lines[1].split()[2], "CODEX")
        self.assertEqual(lines[2].split()[2], "CLAUDE")
        self.assertEqual(lines[3].split()[2], "GROK")


class KeepaliveCliHistoryWiringTests(unittest.TestCase):
    """Every real ping (in --once, continuous, and --service mode) must
    record one chronological history event per agent."""

    def _fake_runner(self, argv, **kwargs):
        return PingResult(True, None)

    def _patched_providers(self):
        return (
            patch.object(keepalive_cli, "ClaudeKeepalive",
                        side_effect=lambda *a, **kw: ClaudeKeepalive(*a, **{**kw, "runner": self._fake_runner})),
            patch.object(keepalive_cli, "CodexKeepalive",
                        side_effect=lambda *a, **kw: CodexKeepalive(*a, **{**kw, "runner": self._fake_runner})),
            patch.object(keepalive_cli, "GrokKeepalive",
                        side_effect=lambda *a, **kw: GrokKeepalive(*a, **{**kw, "runner": self._fake_runner})),
        )

    def test_once_mode_records_one_history_event_per_agent(self):
        fixed = observe.Snapshot("unavailable", None, None, "fixture")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            p1, p2, p3 = self._patched_providers()
            with p1, p2, p3, \
                 patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed), \
                 redirect_stdout(io.StringIO()):
                rc = keepalive_cli.main(["--once", "--state-dir", str(state_dir), "--model", "fixture"])
            self.assertEqual(rc, 0)
            for agent in ("claude", "codex", "grok"):
                events = history.load_events(history.history_path(state_dir, agent))
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["agent"], agent)
                self.assertEqual(events[0]["ping_status"], "ok")

    def test_service_mode_records_history_too(self):
        fixed = observe.Snapshot("unavailable", None, None, "fixture")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            p1, p2, p3 = self._patched_providers()
            from agentstatus.keepalive.core import CycleRunner
            with p1, p2, p3, \
                 patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed), \
                 patch.object(CycleRunner, "run", return_value=0), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = keepalive_cli.main(["--service", "--state-dir", str(state_dir), "--model", "fixture"])
        self.assertEqual(rc, 0)
        # --service patches CycleRunner.run() itself (as the existing
        # dispatch tests do), so no slot/event ever actually fires here --
        # this only confirms the service path starts and returns cleanly
        # with the history-aware callback wired in without error.

    def test_a_failed_ping_is_still_recorded_with_its_error(self):
        def failing_runner(argv, **kwargs):
            return PingResult(False, "402 quota exhausted")

        fixed = observe.Snapshot("ok", None, observe.Window(used_percent=88, resets_at=None))
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with patch.object(keepalive_cli, "GrokKeepalive",
                              side_effect=lambda *a, **kw: GrokKeepalive(*a, **{**kw, "runner": failing_runner})), \
                 patch.object(keepalive_cli, "ClaudeKeepalive",
                              side_effect=lambda *a, **kw: ClaudeKeepalive(*a, **{**kw, "runner": self._fake_runner})), \
                 patch.object(keepalive_cli, "CodexKeepalive",
                              side_effect=lambda *a, **kw: CodexKeepalive(*a, **{**kw, "runner": self._fake_runner})), \
                 patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed), \
                 redirect_stdout(io.StringIO()):
                rc = keepalive_cli.main(["--once", "--state-dir", str(state_dir), "--model", "fixture"])
            self.assertEqual(rc, 0)
            events = history.load_events(history.history_path(state_dir, "grok"))
            self.assertEqual(events[0]["ping_status"], "fail")
            self.assertEqual(events[0]["ping_error"], "402 quota exhausted")
            # Ping failure and provider observation are independent: the
            # provider-native read can still succeed even though this ping failed.
            self.assertEqual(events[0]["observation_status"], "ok")
            self.assertEqual(events[0]["weekly"]["used_percent"], 88)

    def test_history_recording_failure_never_crashes_the_ping_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            p1, p2, p3 = self._patched_providers()
            with p1, p2, p3, \
                 patch.object(keepalive_cli.keepalive_observe, "observe", side_effect=RuntimeError("boom")), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                rc = keepalive_cli.main(["--once", "--state-dir", str(state_dir), "--model", "fixture"])
        self.assertEqual(rc, 0)
        self.assertIn("history recording failed", err.getvalue())

    def test_a_skip_slot_still_records_a_history_event_via_the_real_cli_wiring(self):
        # Exercises the actual cli._record_history() the CycleRunner's
        # on_event callback uses in --service/continuous mode, with a
        # SKIP-shaped record exactly as KeepaliveAgent.run_slot() produces
        # it (see agentstatus/keepalive/core.py).
        fixed = observe.Snapshot("ok", None, observe.Window(used_percent=7, resets_at=None))
        skip_record = {
            "agent": "codex", "action": "skip", "timestamp": iso(dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)),
            "last_activity": iso(dt.datetime(2026, 9, 20, 11, 45, tzinfo=UTC)),
            "ping_status": None, "ping_error": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            args = keepalive_cli.build_parser().parse_args(["--state-dir", str(state_dir)])
            with patch.object(keepalive_cli.keepalive_observe, "observe", return_value=fixed):
                keepalive_cli._record_history("codex", skip_record, args)
            events = history.load_events(history.history_path(state_dir, "codex"))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["action"], "skip")
        self.assertIsNone(event["ping_status"])
        self.assertIsNone(event["ping_error"])
        self.assertEqual(event["last_activity"], skip_record["last_activity"])
        # Provider-native observation still happens on a SKIP slot.
        self.assertEqual(event["observation_status"], "ok")
        self.assertEqual(event["weekly"]["used_percent"], 7)


class KeepaliveHistoryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name)
        base = dt.datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
        history.record_event(history.history_path(self.state_dir, "codex"),
                             make_event("codex", base + dt.timedelta(minutes=33)), now=base)
        history.record_event(history.history_path(self.state_dir, "claude"),
                             make_event("claude", base + dt.timedelta(minutes=18)), now=base)
        history.record_event(history.history_path(self.state_dir, "grok"),
                             make_event("grok", base + dt.timedelta(minutes=30)), now=base)

    def tearDown(self):
        self.temp.cleanup()

    def test_history_command_prints_all_agents_chronologically(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = keepalive_cli.main(["history", "--state-dir", str(self.state_dir)])
        self.assertEqual(rc, 0)
        lines = [l for l in buf.getvalue().splitlines() if l and not l.startswith(" ")]
        agents_in_order = [l.split()[2] for l in lines[1:]]
        self.assertEqual(agents_in_order, ["CLAUDE", "GROK", "CODEX"])

    def test_history_command_agent_filter(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = keepalive_cli.main(["history", "--state-dir", str(self.state_dir), "--agent", "codex"])
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        # Per the display contract, a single-agent view drops the AGENT
        # column entirely (it would be redundant on every row) -- so the
        # filter is verified by event count and by the other two agents'
        # names never appearing, not by "CODEX" appearing as a column value.
        self.assertNotIn("AGENT", output)
        self.assertNotIn("CLAUDE", output)
        self.assertNotIn("GROK", output)
        lines = [l for l in output.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)  # header + exactly one codex event

    def test_history_command_agent_filter_header_matches_the_per_agent_contract(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            keepalive_cli.main(["history", "--state-dir", str(self.state_dir), "--agent", "codex"])
        header = buf.getvalue().splitlines()[0]
        self.assertEqual(header, render.render_header(show_agent=False))
        for label in ("TIME", "ACTION", "LAST ACTIVITY", "PING", "5H", "RESET", "WEEK"):
            self.assertIn(label, header)
        self.assertNotIn("AGENT", header)


class KeepaliveMonitorCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name)
        now = dt.datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
        history.record_event(history.history_path(self.state_dir, "codex"),
                             make_event("codex", now), now=now)

    def tearDown(self):
        self.temp.cleanup()

    def test_monitor_once_renders_existing_history_without_pinging(self):
        with patch.object(keepalive_cli, "ClaudeKeepalive", side_effect=AssertionError), \
             patch.object(keepalive_cli, "CodexKeepalive", side_effect=AssertionError), \
             patch.object(keepalive_cli, "GrokKeepalive", side_effect=AssertionError), \
             redirect_stdout(io.StringIO()) as out:
            rc = keepalive_cli.main(["monitor", "--state-dir", str(self.state_dir), "--once"])
        self.assertEqual(rc, 0)
        self.assertIn("CODEX", out.getvalue())
        self.assertIn("read-only", out.getvalue())

    def test_monitor_never_constructs_a_provider_or_spawns_a_subprocess(self):
        import subprocess as subprocess_module
        real_popen = subprocess_module.Popen

        def forbidden_popen(*args, **kwargs):
            raise AssertionError("keepalive monitor must never spawn a subprocess")

        subprocess_module.Popen = forbidden_popen
        try:
            with redirect_stdout(io.StringIO()):
                rc = keepalive_cli.main(["monitor", "--state-dir", str(self.state_dir), "--once"])
        finally:
            subprocess_module.Popen = real_popen
        self.assertEqual(rc, 0)

    def test_monitor_source_never_references_provider_classes(self):
        # AST-based, not a raw text scan: monitor_main's own docstring
        # legitimately *names* these classes to explain why it can't reach
        # them -- what must never exist is actual code (a Name/Attribute
        # node) referencing one. Docstrings are plain string constants, not
        # Name/Attribute nodes, so this cannot be fooled by that prose.
        import ast
        import inspect
        import textwrap
        forbidden = {"ClaudeKeepalive", "CodexKeepalive", "GrokKeepalive", "KeepaliveAgent",
                    "ping", "ping_once", "run"}
        source = textwrap.dedent(inspect.getsource(keepalive_cli.monitor_main))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden:
                self.fail(f"monitor_main references {node.id!r} in actual code")
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                self.fail(f"monitor_main references .{node.attr} in actual code")

    def test_monitor_never_writes_to_any_history_file(self):
        path = history.history_path(self.state_dir, "codex")
        before = path.read_bytes()
        with redirect_stdout(io.StringIO()):
            keepalive_cli.main(["monitor", "--state-dir", str(self.state_dir), "--once"])
        after = path.read_bytes()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
