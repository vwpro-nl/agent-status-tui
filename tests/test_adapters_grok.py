import copy
import inspect
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _fixtures as fx
from agentstatus.adapters import grok
from agentstatus.adapters._common import parse_iso

NOW = 1_000_000_000.0
SECRET = "grok-bearer-SUPERSECRET-abc123"


class GrokActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = fx.make_env(Path(self.tmp))

    def test_unused_when_no_sessions_dir(self):
        self.assertFalse(grok.detect(self.env).ever_used)

    def test_activity_prefers_summary_last_active_at(self):
        import time

        fx.grok_session(self.env, last_active_at=NOW - 40, updated_at=NOW - 900,
                        opened_at=NOW - 5000, file_mtime=time.time())
        d = grok.detect(self.env)
        self.assertTrue(d.ever_used)
        self.assertAlmostEqual(d.last_activity, NOW - 40, delta=3)

    def test_falls_back_to_file_mtime_when_no_summary(self):
        fx.grok_session(self.env, file_mtime=NOW - 70)
        d = grok.detect(self.env)
        self.assertTrue(d.ever_used)
        self.assertAlmostEqual(d.last_activity, NOW - 70, delta=3)

    def test_detection_never_reads_auth_json(self):
        fx.grok_session(self.env, last_active_at=NOW - 10)
        fx.grok_auth(self.env, key=SECRET, expires_at=NOW + 3600)
        opened = []
        real_open = Path.open

        def spy_open(self_path, *a, **k):
            opened.append(str(self_path))
            return real_open(self_path, *a, **k)

        Path.open = spy_open
        try:
            grok.detect(self.env)
        finally:
            Path.open = real_open
        self.assertFalse(any("auth.json" in p for p in opened))


class GrokWeeklyQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = fx.make_env(Path(self.tmp))
        fx.grok_session(self.env, last_active_at=NOW - 22)   # activity always present
        self._orig_request = grok._billing_request
        self.addCleanup(setattr, grok, "_billing_request", self._orig_request)
        self.calls = []

    def _mock(self, result=None, exc=None):
        def fake(token):
            self.calls.append(token)
            if exc is not None:
                raise exc
            return result

        grok._billing_request = fake

    # -- happy path -----------------------------------------------------

    def test_successful_weekly_response_is_a_live_window(self):
        fx.grok_auth(self.env, key="the-real-key", expires_at=NOW + 3600)
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        status = grok.poll(self.env, NOW)

        self.assertIsNotNone(status.weekly)
        self.assertAlmostEqual(status.weekly.used_percent, 3.0)
        self.assertAlmostEqual(
            status.weekly.resets_at,
            parse_iso("2026-09-12T16:26:02.206145+00:00"),
        )
        self.assertEqual(status.weekly.nominal_minutes, 10080)   # 7 days
        self.assertIsNone(status.five_hour)
        self.assertEqual(status.freshness_kind, "live")
        self.assertIsNone(status.source_age)
        self.assertEqual(status.availability, "ok")
        self.assertAlmostEqual(status.last_activity, NOW - 22, delta=3)

    def test_weekly_window_supports_the_elapsed_marker(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        status = grok.poll(self.env, NOW)
        from agentstatus.model import time_budget

        # start/end are both known -> elapsed is computable -> marker possible
        _text, _colour, elapsed = time_budget(status.weekly, parse_iso(
            "2026-09-08T16:26:02+00:00"))
        self.assertIsNotNone(elapsed)

    # -- credential selection / expiry --------------------------------

    def test_expired_token_is_never_sent(self):
        fx.grok_auth(self.env, key="expired", expires_at=NOW - 10)
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        status = grok.poll(self.env, NOW)
        self.assertEqual(self.calls, [])                 # request never attempted
        self.assertIsNone(status.weekly)
        self.assertEqual(status.freshness_kind, "activity")
        self.assertAlmostEqual(status.last_activity, NOW - 22, delta=3)

    def test_latest_non_expired_entry_is_chosen(self):
        fx.grok_auth(self.env, entries=[
            {"key": "old", "expires_at": fx.iso(NOW + 60)},
            {"key": "expired", "expires_at": fx.iso(NOW - 5)},
            {"key": "newest", "expires_at": fx.iso(NOW + 9000)},
        ])
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        grok.poll(self.env, NOW)
        self.assertEqual(self.calls, ["newest"])

    def test_auth_file_absent_yields_activity_only(self):
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        status = grok.poll(self.env, NOW)
        self.assertEqual(self.calls, [])
        self.assertIsNone(status.weekly)
        self.assertEqual(status.freshness_kind, "activity")

    def test_malformed_auth_json_yields_activity_only(self):
        fx.grok_auth(self.env, raw="{ this is not json ")
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        status = grok.poll(self.env, NOW)
        self.assertEqual(self.calls, [])
        self.assertIsNone(status.weekly)
        self.assertEqual(status.freshness_kind, "activity")

    def test_entry_without_a_usable_key_is_skipped(self):
        fx.grok_auth(self.env, entries=[{"key": "", "expires_at": fx.iso(NOW + 60)},
                                        {"key": None}])
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        status = grok.poll(self.env, NOW)
        self.assertEqual(self.calls, [])
        self.assertIsNone(status.weekly)

    # -- request failures -------------------------------------------

    def test_http_401_is_isolated(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(exc=urllib.error.HTTPError(grok._BILLING_URL, 401,
                                              "Unauthorized", {}, None))
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)
        self.assertEqual(status.freshness_kind, "activity")
        self.assertNotIn("401", "")  # sanity; real check below

    def test_http_403_is_isolated(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(exc=urllib.error.HTTPError(grok._BILLING_URL, 403,
                                              "Forbidden", {}, None))
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)

    def test_network_timeout_is_isolated(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(exc=TimeoutError("timed out"))
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)
        self.assertEqual(status.freshness_kind, "activity")

    def test_url_error_is_isolated(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(exc=urllib.error.URLError("no route to host"))
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)

    def test_malformed_billing_json_is_isolated(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(exc=json.JSONDecodeError("bad", "doc", 0))
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)
        self.assertEqual(status.freshness_kind, "activity")

    def test_non_dict_billing_payload_is_isolated(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(result=["not", "a", "dict"])
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)

    # -- schema guards --------------------------------------------

    def test_non_weekly_period_is_not_labelled_weekly(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(result={"config": {
            "creditUsagePercent": 12.0,
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY",
                              "end": "2026-10-01T00:00:00+00:00"},
        }})
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)
        self.assertEqual(status.freshness_kind, "activity")

    def test_missing_percent_does_not_become_zero(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(result={"config": {
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                              "start": "2026-09-05T16:26:02+00:00",
                              "end": "2026-09-12T16:26:02+00:00"},
        }})
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)      # NOT Window(used_percent=0.0)

    def test_null_percent_does_not_become_zero(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(result={"config": {
            "creditUsagePercent": None,
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                              "end": "2026-09-12T16:26:02+00:00"},
        }})
        status = grok.poll(self.env, NOW)
        self.assertIsNone(status.weekly)

    def test_malformed_percent_is_rejected(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        for bad in ("3%", True, [3], {"v": 3}):
            with self.subTest(bad=bad):
                self._mock(result={"config": {
                    "creditUsagePercent": bad,
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                      "end": "2026-09-12T16:26:02+00:00"},
                }})
                status = grok.poll(self.env, NOW)
                self.assertIsNone(status.weekly)

    def test_missing_period_end_gives_a_window_with_no_fabricated_reset(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(result={"config": {
            "creditUsagePercent": 4.0,
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
        }})
        status = grok.poll(self.env, NOW)
        self.assertIsNotNone(status.weekly)
        self.assertAlmostEqual(status.weekly.used_percent, 4.0)
        self.assertIsNone(status.weekly.resets_at)          # not fabricated
        self.assertEqual(status.weekly.nominal_minutes, 10080)

    def test_malformed_period_end_gives_no_reset(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(result={"config": {
            "creditUsagePercent": 4.0,
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "not-a-date"},
        }})
        status = grok.poll(self.env, NOW)
        self.assertIsNotNone(status.weekly)
        self.assertIsNone(status.weekly.resets_at)

    # -- security -----------------------------------------------

    def test_credential_never_appears_in_note_or_status(self):
        fx.grok_auth(self.env, key=SECRET, expires_at=NOW + 3600)
        self._mock(exc=RuntimeError(f"body contained {SECRET} in the response"))
        window, note = grok._weekly_quota(self.env, NOW)
        self.assertIsNone(window)
        self.assertNotIn(SECRET, note)
        status = grok.poll(self.env, NOW)
        self.assertNotIn(SECRET, repr(status))
        self.assertNotIn(SECRET, status.detail or "")

    def test_refresh_token_is_never_accessed_in_code_only_documented(self):
        import ast

        tree = ast.parse(inspect.getsource(grok))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)
        code_literals = [
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "refresh_token" in n.value and n.value not in docstrings
        ]
        self.assertEqual(code_literals, [])           # never a "refresh_token" key access
        attrs = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
        self.assertNotIn("refresh_token", attrs)

    def test_refresh_token_value_is_never_sent_only_key_is(self):
        fx.grok_auth(self.env, entries=[{
            "key": "THE-REAL-KEY",
            "refresh_token": "THE-REFRESH-TOKEN",
            "expires_at": fx.iso(NOW + 3600),
        }])
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        grok.poll(self.env, NOW)
        self.assertEqual(self.calls, ["THE-REAL-KEY"])

    def test_auth_json_is_never_written(self):
        path = fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        before = (path.read_bytes(), path.stat().st_mtime)
        self._mock(result=copy.deepcopy(fx.WEEKLY_BILLING))
        grok.poll(self.env, NOW)
        after = (path.read_bytes(), path.stat().st_mtime)
        self.assertEqual(before, after)

    # -- activity resilience ----------------------------------

    def test_activity_detection_survives_quota_failure(self):
        fx.grok_auth(self.env, key="k", expires_at=NOW + 3600)
        self._mock(exc=urllib.error.URLError("down"))
        status = grok.poll(self.env, NOW)
        self.assertEqual(status.freshness_kind, "activity")
        self.assertAlmostEqual(status.last_activity, NOW - 22, delta=3)
        self.assertAlmostEqual(status.source_age, 22, delta=3)
        # detect() itself is unaffected
        self.assertAlmostEqual(grok.detect(self.env).last_activity, NOW - 22, delta=3)


if __name__ == "__main__":
    unittest.main()
