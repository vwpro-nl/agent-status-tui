import importlib
import os
import tempfile
import unittest

from agentstatus import model


class SharedColorsTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MONITOR_DASHBOARD_COLORS", None)
        importlib.reload(model)

    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        os.environ["MONITOR_DASHBOARD_COLORS"] = f.name
        return importlib.reload(model)

    def test_values_from_the_shared_file(self):
        m = self.load("# x\ntitle=82\nmuted=#010203\nok=#0a0b0c\npending=7\ndown=#ffffff\n")
        self.assertEqual(m.CYAN, "\x1b[38;5;82m")
        self.assertEqual(m.MUTED, "\x1b[38;2;1;2;3m")
        self.assertEqual(m.BAR_FILL_OK, "\x1b[48;2;10;11;12m")
        self.assertEqual(m.BAR_FILL_WARN, "\x1b[48;5;7m")
        self.assertEqual(m.BAR_FILL_HIGH, "\x1b[48;2;255;255;255m")

    def test_built_in_values_without_the_file_or_with_bad_lines(self):
        m = self.load("ok=green\ntitle=300\n")
        self.assertEqual(m.CYAN, "\x1b[38;5;81m")
        self.assertEqual(m.BAR_FILL_OK, "\x1b[48;2;20;105;50m")
        os.environ["MONITOR_DASHBOARD_COLORS"] = ""
        m = importlib.reload(model)
        self.assertEqual(m.MUTED, "\x1b[38;5;244m")


if __name__ == "__main__":
    unittest.main()
