import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentstatus import cli


class CliTests(unittest.TestCase):
    def test_cursor_is_parked_inside_exact_62_by_7_frame(self):
        frame = "\n".join(" " * 62 for _ in range(7))
        real_size = cli.shutil.get_terminal_size
        cli.shutil.get_terminal_size = lambda fallback: os.terminal_size((62, 7))
        try:
            self.assertEqual(cli._park_cursor(frame), "\x1b[7;61H")
        finally:
            cli.shutil.get_terminal_size = real_size

    def test_once_renders_a_single_frame_from_a_single_collect(self):
        calls = {"collect": 0}
        real_collect = cli.collect

        def counting_collect(env, now, previous=None):
            calls["collect"] += 1
            return []

        cli.collect = counting_collect
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["--once"])
        finally:
            cli.collect = real_collect

        self.assertEqual(rc, 0)
        self.assertEqual(calls["collect"], 1)
        self.assertIn("AGENT STATUS", buf.getvalue())

    def test_refresh_is_gated_on_next_refresh_at_not_every_heartbeat(self):
        # A direct read of the loop's intent: collect runs once at startup and
        # then only when time.time() >= next_refresh_at.  We assert the source
        # guards the refresh call with that comparison.
        import inspect

        source = inspect.getsource(cli.main)
        self.assertIn("time.time() >= next_refresh_at", source)
        self.assertIn("tick_deadline = time.monotonic() + 1.0", source)

    def test_normal_status_route_never_imports_or_runs_calibrator(self):
        import subprocess
        real_popen = subprocess.Popen
        def forbidden_probe(*args, **kwargs):
            raise AssertionError("normal status route started a subprocess/probe")
        real_collect = cli.collect
        cli.collect = lambda env, now, previous=None: []
        subprocess.Popen = forbidden_probe
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--once"]), 0)
        finally:
            cli.collect = real_collect
            subprocess.Popen = real_popen
        import inspect
        self.assertIn('effective_argv[0] == "calibrator"', inspect.getsource(cli.main))

    def test_normal_status_route_never_imports_calibrator_module(self):
        # A real, isolated-interpreter check: any calibrator submodule
        # already sitting in sys.modules from another test file would make
        # an in-process check meaningless, so this runs the normal status
        # route in a fresh subprocess and inspects sys.modules there.
        import subprocess
        import sys
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from agentstatus import cli\n"
            "cli.collect = lambda env, now, previous=None: []\n"
            "cli.main(['--once'])\n"
            "assert not any(name == 'agentstatus.calibrator' or name.startswith('agentstatus.calibrator.') "
            "for name in sys.modules), sorted(sys.modules)\n"
            "print('OK')\n"
        ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
