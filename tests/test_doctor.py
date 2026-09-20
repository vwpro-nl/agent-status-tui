"""Doctor tests. All systemd/state interaction is simulated: no real
systemctl call, no real provider CLI, and the live keepalive service/state
on this machine is never touched."""

import datetime as dt
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentstatus import doctor
from agentstatus.keepalive import history as keepalive_history
from agentstatus.keepalive.persistence import STATE_SCHEMA, save_state

UTC = dt.timezone.utc


class KeepalivePackageAvailableTests(unittest.TestCase):
    def test_reports_ok_when_importable(self):
        name, status, detail = doctor.check_keepalive_package_available()
        self.assertEqual(name, "keepalive package")
        self.assertIs(status, True)
        self.assertIn("keepalive", detail)


class UnitFileInstalledTests(unittest.TestCase):
    def test_missing_unit_file_is_warn_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "agent-status-keepalive.service"
            name, status, detail = doctor.check_unit_file_installed(missing)
        self.assertEqual(name, doctor.UNIT_NAME)
        self.assertEqual(status, "warn")
        self.assertIn("not installed", detail)

    def test_present_unit_file_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text("[Unit]\n")
            name, status, detail = doctor.check_unit_file_installed(unit)
        self.assertIs(status, True)
        self.assertEqual(detail, str(unit))


class UnitEnabledActiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.unit = Path(self.temp.name) / "agent-status-keepalive.service"
        self.unit.write_text("[Unit]\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_unit_short_circuits_to_warn(self):
        missing = Path(self.temp.name) / "does-not-exist.service"
        name, status, detail = doctor.check_unit_enabled(missing)
        self.assertEqual(status, "warn")
        self.assertIn("not installed", detail)
        name, status, detail = doctor.check_unit_active(missing)
        self.assertEqual(status, "warn")

    def test_enabled_and_active_is_ok(self):
        def fake_run(cmd, **kwargs):
            import subprocess
            out = "enabled" if "is-enabled" in cmd else "active"
            return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        with patch.object(doctor.subprocess, "run", side_effect=fake_run):
            name, status, detail = doctor.check_unit_enabled(self.unit)
            self.assertIs(status, True)
            name, status, detail = doctor.check_unit_active(self.unit)
            self.assertIs(status, True)

    def test_disabled_unit_is_warn(self):
        def fake_run(cmd, **kwargs):
            import subprocess
            return subprocess.CompletedProcess(cmd, 1, "disabled\n", "")
        with patch.object(doctor.subprocess, "run", side_effect=fake_run):
            _name, status, _detail = doctor.check_unit_enabled(self.unit)
        self.assertEqual(status, "warn")

    def test_failed_active_state_is_a_real_failure(self):
        def fake_run(cmd, **kwargs):
            import subprocess
            return subprocess.CompletedProcess(cmd, 3, "failed\n", "")
        with patch.object(doctor.subprocess, "run", side_effect=fake_run):
            _name, status, detail = doctor.check_unit_active(self.unit)
        self.assertIs(status, False)
        self.assertIn("journalctl", detail)

    def test_inactive_while_enabled_is_warn_not_fail(self):
        # e.g. no user session currently running -- not necessarily wrong.
        def fake_run(cmd, **kwargs):
            import subprocess
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        with patch.object(doctor.subprocess, "run", side_effect=fake_run):
            _name, status, _detail = doctor.check_unit_active(self.unit)
        self.assertEqual(status, "warn")

    def test_systemctl_unavailable_is_unknown_not_fail(self):
        def raising_run(cmd, **kwargs):
            raise OSError("systemctl not found")
        with patch.object(doctor.subprocess, "run", side_effect=raising_run):
            _name, status, detail = doctor.check_unit_enabled(self.unit)
        self.assertEqual(status, "unknown")
        self.assertIn("systemctl", detail)

    def test_never_calls_systemctl_with_enable_start_stop_or_disable(self):
        calls = []
        def fake_run(cmd, **kwargs):
            import subprocess
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "active\n", "")
        with patch.object(doctor.subprocess, "run", side_effect=fake_run):
            doctor.check_unit_enabled(self.unit)
            doctor.check_unit_active(self.unit)
        for call in calls:
            self.assertNotIn("enable", call)
            self.assertNotIn("disable", call)
            self.assertNotIn("start", call)
            self.assertNotIn("stop", call)
            self.assertNotIn("restart", call)


class ExecutableCoherenceTests(unittest.TestCase):
    def test_missing_unit_is_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "agent-status-keepalive.service"
            _name, status, _detail = doctor.check_executable_coherence(missing)
        self.assertEqual(status, "warn")

    FULL_ARGS = ('keepalive --service --claude-command "claude" '
                '--codex-command "codex" --grok-command "grok"')

    def test_execstart_pointing_at_this_checkouts_entrypoint_is_ok(self):
        entrypoint = doctor.repo_root() / "agent-status-tui"
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text(f'ExecStart="{entrypoint}" {self.FULL_ARGS}\n')
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertIs(status, True)
        self.assertEqual(detail, str(entrypoint))

    def test_execstart_pointing_at_a_different_checkout_is_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "agent-status-tui"
            other.write_text("#!/usr/bin/env python3\n")
            other.chmod(0o755)
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text(f'ExecStart="{other}" {self.FULL_ARGS}\n')
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertEqual(status, "warn")
        self.assertIn("different checkout", detail)

    def test_execstart_without_keepalive_service_is_a_failure(self):
        # Regression: previously only the executable path was checked, so a
        # unit that ran the plain entrypoint with no arguments at all (which
        # starts the interactive status TUI, not keepalive) was reported OK.
        entrypoint = doctor.repo_root() / "agent-status-tui"
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text(f'ExecStart="{entrypoint}"\n')
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertIs(status, False)
        self.assertIn("keepalive --service", detail)

    def test_execstart_missing_service_flag_is_a_failure(self):
        entrypoint = doctor.repo_root() / "agent-status-tui"
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text(f'ExecStart="{entrypoint}" keepalive\n')
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertIs(status, False)

    def test_execstart_missing_one_provider_command_flag_is_warn(self):
        entrypoint = doctor.repo_root() / "agent-status-tui"
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text(
                f'ExecStart="{entrypoint}" keepalive --service '
                f'--claude-command "claude" --codex-command "codex"\n'
            )
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertEqual(status, "warn")
        self.assertIn("--grok-command", detail)

    def test_execstart_with_lookalike_command_and_flag_is_rejected(self):
        # Regression: substring matching let "keepalive" match inside
        # "notkeepalive" and "--service" match inside "--service-bogus".
        # Exact-token matching (via shlex) must reject both.
        entrypoint = doctor.repo_root() / "agent-status-tui"
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text(f'ExecStart="{entrypoint}" notkeepalive --service-bogus\n')
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertIs(status, False)
        self.assertIn("keepalive --service", detail)

    def test_execstart_lookalike_provider_flag_still_counts_as_missing(self):
        # "--claude-command-extra" must not satisfy "--claude-command" via
        # substring matching -- it should still be reported as missing.
        entrypoint = doctor.repo_root() / "agent-status-tui"
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text(
                f'ExecStart="{entrypoint}" keepalive --service '
                f'--claude-command-extra "x" --codex-command "codex" --grok-command "grok"\n'
            )
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertEqual(status, "warn")
        self.assertIn("--claude-command", detail)

    def test_execstart_target_missing_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text('ExecStart="/does/not/exist/agent-status-tui" keepalive --service\n')
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertIs(status, False)
        self.assertIn("does not exist", detail)

    def test_no_execstart_line_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text("[Unit]\nDescription=x\n")
            _name, status, detail = doctor.check_executable_coherence(unit)
        self.assertIs(status, False)
        self.assertIn("ExecStart", detail)


class ConfigurationChecksTests(unittest.TestCase):
    def test_cycle_sourced_from_the_real_implementation(self):
        # Doctor must read the live constants, not hardcode its own copy.
        from agentstatus.keepalive.core import AGENT_STAGGER_SECONDS, CYCLE_SECONDS
        _name, status, detail = doctor.check_cycle_configuration()
        self.assertIs(status, True)
        self.assertIn(str(AGENT_STAGGER_SECONDS), detail)
        self.assertEqual(CYCLE_SECONDS, 1800)

    def test_cycle_mismatch_would_be_reported_as_a_failure(self):
        with patch.object(doctor, "CYCLE_SECONDS", 3000):
            _name, status, detail = doctor.check_cycle_configuration()
        self.assertIs(status, False)
        self.assertIn("3000", detail)
        self.assertIn("1800", detail)

    def test_stagger_mismatch_would_be_reported_as_a_failure(self):
        with patch.object(doctor, "AGENT_STAGGER_SECONDS", 7):
            _name, status, detail = doctor.check_cycle_configuration()
        self.assertIs(status, False)
        self.assertIn("7", detail)
        self.assertIn("5", detail)

    def test_providers_present_matches_the_real_implementation(self):
        from agentstatus.keepalive.cli import AGENTS
        _name, status, detail = doctor.check_providers_present()
        self.assertIs(status, True)
        for agent in AGENTS:
            self.assertIn(agent.upper(), detail)

    def test_providers_mismatch_would_be_reported_as_a_failure(self):
        with patch.object(doctor, "KEEPALIVE_AGENTS", ("claude", "codex")):
            _name, status, detail = doctor.check_providers_present()
        self.assertIs(status, False)


class ProviderStateChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_never_pinged_provider_is_warn_not_fail(self):
        _name, status, detail = doctor.check_provider_state("codex", self.root)
        self.assertEqual(status, "warn")
        self.assertIn("never pinged", detail)

    def test_valid_state_is_ok_regardless_of_last_ping_outcome(self):
        # A failed last ping (e.g. provider quota exhausted) is a provider
        # availability fact, never a Doctor installation finding.
        save_state(self.root / "grok.json", {
            "schema": STATE_SCHEMA, "agent": "grok",
            "last_ping_finished_at": "2026-09-19T03:14:03Z",
            "last_status": "fail", "last_error": "402 quota exhausted",
            "updated_at": "2026-09-19T03:14:03Z",
        })
        _name, status, detail = doctor.check_provider_state("grok", self.root)
        self.assertIs(status, True)
        self.assertIn("last_status=fail", detail)

    def test_old_but_syntactically_valid_state_is_still_ok(self):
        save_state(self.root / "claude.json", {
            "schema": STATE_SCHEMA, "agent": "claude",
            "last_ping_finished_at": "2020-01-01T00:00:00Z",
            "last_status": "ok", "last_error": None,
            "updated_at": "2020-01-01T00:00:00Z",
        })
        _name, status, _detail = doctor.check_provider_state("claude", self.root)
        self.assertIs(status, True)

    def test_corrupt_state_file_is_a_failure(self):
        path = self.root / "codex.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")
        _name, status, detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, False)
        self.assertIn("not structurally valid", detail)

    def test_wrong_schema_state_file_is_a_failure(self):
        path = self.root / "codex.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": "something-else/v1", "agent": "codex"}')
        _name, status, detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, False)

    def test_identity_mismatch_is_a_failure(self):
        save_state(self.root / "codex.json", {
            "schema": STATE_SCHEMA, "agent": "not-codex",
            "last_ping_finished_at": None, "last_status": None,
            "last_error": None, "updated_at": None,
        })
        _name, status, detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, False)
        self.assertIn("identity mismatch", detail)

    def test_malformed_last_ping_finished_at_is_a_failure(self):
        # Regression: schema + identity alone called this "structurally
        # valid" even though it violates the field's own documented
        # schema (a valid, timezone-aware ISO timestamp or null) -- the one
        # doctor/diagnostic surface for "when did this agent last ping".
        save_state(self.root / "codex.json", {
            "schema": STATE_SCHEMA, "agent": "codex",
            "last_ping_finished_at": "not-a-timestamp",
            "last_status": "ok", "last_error": None, "updated_at": None,
        })
        _name, status, detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, False)
        self.assertIn("last_ping_finished_at", detail)

    def test_non_string_last_ping_finished_at_is_a_failure(self):
        save_state(self.root / "codex.json", {
            "schema": STATE_SCHEMA, "agent": "codex",
            "last_ping_finished_at": 12345,
            "last_status": "ok", "last_error": None, "updated_at": None,
        })
        _name, status, detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, False)
        self.assertIn("last_ping_finished_at", detail)

    def test_null_last_ping_finished_at_is_still_ok(self):
        # A never-pinged-yet agent may legitimately have None here -- the
        # scheduler itself treats None as "use now", not an error.
        save_state(self.root / "codex.json", {
            "schema": STATE_SCHEMA, "agent": "codex",
            "last_ping_finished_at": None,
            "last_status": None, "last_error": None, "updated_at": None,
        })
        _name, status, _detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, True)

    def test_valid_iso_timestamp_matching_scheduler_parsing_is_ok(self):
        save_state(self.root / "codex.json", {
            "schema": STATE_SCHEMA, "agent": "codex",
            "last_ping_finished_at": "2026-09-19T03:14:05.378843Z",
            "last_status": "ok", "last_error": None, "updated_at": None,
        })
        _name, status, _detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, True)

    def test_naive_timestamp_without_timezone_is_a_failure(self):
        # Regression: dt.datetime.fromisoformat() parses this into a naive
        # datetime instead of raising, so schema+parseability alone called
        # it "valid" even though the scheduler works exclusively in
        # timezone-aware UTC and would raise on the first comparison.
        save_state(self.root / "codex.json", {
            "schema": STATE_SCHEMA, "agent": "codex",
            "last_ping_finished_at": "2026-09-19T03:14:03",
            "last_status": "ok", "last_error": None, "updated_at": None,
        })
        _name, status, detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, False)
        self.assertIn("timezone-aware", detail)

    def test_equivalent_timezone_aware_timestamp_is_ok(self):
        save_state(self.root / "codex.json", {
            "schema": STATE_SCHEMA, "agent": "codex",
            "last_ping_finished_at": "2026-09-19T03:14:03+00:00",
            "last_status": "ok", "last_error": None, "updated_at": None,
        })
        _name, status, _detail = doctor.check_provider_state("codex", self.root)
        self.assertIs(status, True)

    def test_grok_402_with_a_valid_aware_timestamp_is_still_ok(self):
        # Confirms the timezone fix does not turn a normal provider
        # failure into a Doctor infrastructure fault.
        save_state(self.root / "grok.json", {
            "schema": STATE_SCHEMA, "agent": "grok",
            "last_ping_finished_at": "2026-09-19T03:14:03.605197Z",
            "last_status": "fail", "last_error": "402 quota exhausted",
            "updated_at": "2026-09-19T03:14:03.605197Z",
        })
        _name, status, _detail = doctor.check_provider_state("grok", self.root)
        self.assertIs(status, True)


class HistoryDirectoryChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_history_dir_is_warn_not_fail(self):
        _name, status, detail = doctor.check_history_dir_writable(self.root)
        self.assertEqual(status, "warn")
        self.assertIn("not created yet", detail)

    def test_present_writable_history_dir_is_ok(self):
        (self.root / "history").mkdir()
        _name, status, _detail = doctor.check_history_dir_writable(self.root)
        self.assertIs(status, True)

    def test_history_path_that_is_a_file_not_a_directory_is_a_failure(self):
        (self.root / "history").write_text("not a directory")
        _name, status, detail = doctor.check_history_dir_writable(self.root)
        self.assertIs(status, False)
        self.assertIn("not a directory", detail)

    def test_unwritable_history_dir_is_a_failure(self):
        history_dir = self.root / "history"
        history_dir.mkdir()
        history_dir.chmod(0o500)
        try:
            _name, status, detail = doctor.check_history_dir_writable(self.root)
        finally:
            history_dir.chmod(0o700)  # restore so tearDown can clean it up
        self.assertIs(status, False)
        self.assertIn("not writable", detail)


class HistoryFileChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_history_file_yet_is_warn(self):
        _name, status, detail = doctor.check_history_file("codex", self.root)
        self.assertEqual(status, "warn")
        self.assertIn("no history yet", detail)

    def test_valid_recent_history_is_ok(self):
        now = dt.datetime(2026, 9, 20, tzinfo=UTC)
        path = keepalive_history.history_path(self.root, "codex")
        keepalive_history.record_event(path, {
            "timestamp": now.isoformat().replace("+00:00", "Z"), "agent": "codex",
            "ping_status": "ok", "ping_error": None, "five_hour": None, "weekly": None,
            "observation_status": "unavailable", "observation_detail": "fixture",
        }, now=now)
        _name, status, detail = doctor.check_history_file("codex", self.root, now=now)
        self.assertIs(status, True)
        self.assertIn("1 event", detail)

    def test_corrupt_lines_are_reported_as_warn_not_fail_and_self_heal_is_mentioned(self):
        path = keepalive_history.history_path(self.root, "codex")
        path.parent.mkdir(parents=True)
        path.write_text("not valid json at all\n")
        _name, status, detail = doctor.check_history_file("codex", self.root)
        self.assertEqual(status, "warn")
        self.assertIn("could not be parsed", detail)

    def test_mixed_valid_and_corrupt_lines_is_ok_but_mentions_skipped_count(self):
        import json
        now = dt.datetime(2026, 9, 20, tzinfo=UTC)
        path = keepalive_history.history_path(self.root, "codex")
        path.parent.mkdir(parents=True)
        good = {
            "schema": keepalive_history.HISTORY_SCHEMA,
            "timestamp": now.isoformat().replace("+00:00", "Z"), "agent": "codex",
            "ping_status": "ok", "ping_error": None, "five_hour": None, "weekly": None,
            "observation_status": "unavailable", "observation_detail": None,
        }
        path.write_text("garbage\n" + json.dumps(good) + "\n")
        _name, status, detail = doctor.check_history_file("codex", self.root, now=now)
        self.assertIs(status, True)
        self.assertIn("unparseable line(s) skipped", detail)

    def test_history_older_than_retention_plus_slack_is_warn(self):
        now = dt.datetime(2026, 9, 20, tzinfo=UTC)
        very_old = now - dt.timedelta(days=keepalive_history.RETENTION_DAYS + 5)
        path = keepalive_history.history_path(self.root, "codex")
        # Write directly (bypassing record_event's own retention pruning) to
        # simulate housekeeping having stopped running for this agent.
        import json
        path.parent.mkdir(parents=True)
        event = {
            "schema": keepalive_history.HISTORY_SCHEMA,
            "timestamp": very_old.isoformat().replace("+00:00", "Z"), "agent": "codex",
            "ping_status": "ok", "ping_error": None, "five_hour": None, "weekly": None,
            "observation_status": "unavailable", "observation_detail": None,
        }
        path.write_text(json.dumps(event) + "\n")
        _name, status, detail = doctor.check_history_file("codex", self.root, now=now)
        self.assertEqual(status, "warn")
        self.assertIn("retention", detail)

    def test_doctor_never_deletes_or_rewrites_a_history_file(self):
        path = keepalive_history.history_path(self.root, "codex")
        path.parent.mkdir(parents=True)
        path.write_text("garbage\n")
        before = path.read_bytes()
        doctor.check_history_file("codex", self.root)
        after = path.read_bytes()
        self.assertEqual(before, after)


class DoctorCliTests(unittest.TestCase):
    def test_all_ok_and_warn_exits_zero(self):
        # A virgin/never-installed keepalive is entirely warns -- doctor
        # must not treat a normal first-time state as a hard failure.
        with tempfile.TemporaryDirectory() as tmp:
            missing_unit = Path(tmp) / "agent-status-keepalive.service"
            empty_state_dir = Path(tmp) / "state"
            with patch.object(doctor, "systemd_user_unit_path", return_value=missing_unit), \
                 patch.object(doctor, "keepalive_state_root", return_value=empty_state_dir), \
                 redirect_stdout(io.StringIO()) as out:
                rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("[WARN]", out.getvalue())
        self.assertNotIn("[FAIL]", out.getvalue())

    def test_a_real_failure_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "agent-status-keepalive.service"
            unit.write_text('ExecStart="/does/not/exist" keepalive --service\n')
            empty_state_dir = Path(tmp) / "state"

            def fake_run(cmd, **kwargs):
                import subprocess
                return subprocess.CompletedProcess(cmd, 0, "enabled\n", "")

            with patch.object(doctor, "systemd_user_unit_path", return_value=unit), \
                 patch.object(doctor, "keepalive_state_root", return_value=empty_state_dir), \
                 patch.object(doctor.subprocess, "run", side_effect=fake_run), \
                 redirect_stdout(io.StringIO()) as out:
                rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL]", out.getvalue())

    def test_doctor_never_imports_calibrator(self):
        # AST-based, not a raw text scan: the module's own docstring
        # legitimately *mentions* that it never imports calibrator -- what
        # must never exist is an actual import statement doing so.
        import ast
        tree = ast.parse(Path(doctor.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "calibrator" in node.module:
                self.fail(f"doctor.py imports from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("calibrator", alias.name)

    def test_doctor_never_invokes_a_provider_cli_or_subprocess_other_than_systemctl(self):
        import inspect
        source = inspect.getsource(doctor)
        # The only subprocess surface doctor has at all is the read-only
        # systemctl wrapper -- there is no direct call to claude/codex/grok.
        self.assertNotIn("claude_command", source)
        self.assertNotIn(".probe(", source)
        self.assertNotIn("Popen", source)


if __name__ == "__main__":
    unittest.main()
