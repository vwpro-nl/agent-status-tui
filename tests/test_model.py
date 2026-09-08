import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentstatus import model
from agentstatus.model import Window, age_text, bar, time_budget
from agentstatus.render import visible_len

NOW = 1_000_000_000.0


class AgeTextTests(unittest.TestCase):
    def test_compact_forms(self):
        self.assertEqual(age_text(16), "16s")
        self.assertEqual(age_text(59), "59s")
        self.assertEqual(age_text(60), "1m")
        self.assertEqual(age_text(192), "3m")
        self.assertEqual(age_text(660), "11m")
        self.assertEqual(age_text(7500), "2h 5m")
        self.assertEqual(age_text(280800), "3d 6h")

    def test_no_composite_seconds_over_a_minute(self):
        self.assertNotIn("s", age_text(192))

    def test_none_is_double_dash(self):
        self.assertEqual(age_text(None), "--")


class TimeBudgetTests(unittest.TestCase):
    def test_missing_window_is_double_dash(self):
        text, colour, elapsed = time_budget(None, NOW)
        self.assertEqual(text, "--")
        self.assertIsNone(elapsed)

    def test_ready_semantic_zero_percent_no_reset(self):
        w = Window(used_percent=0.0, resets_at=None, nominal_minutes=300)
        text, _c, elapsed = time_budget(w, NOW)
        self.assertEqual(text, "ready")
        self.assertIsNone(elapsed)

    def test_nonzero_usage_without_reset_is_unknown_not_invented(self):
        w = Window(used_percent=5.0, resets_at=None, nominal_minutes=300)
        text, _c, _e = time_budget(w, NOW)
        self.assertEqual(text, "unknown")

    def test_missing_usage_without_reset_is_unknown(self):
        w = Window(used_percent=None, resets_at=None, nominal_minutes=300)
        text, _c, _e = time_budget(w, NOW)
        self.assertEqual(text, "unknown")

    def test_unstarted_window_zero_percent_rolling_reset_is_clean_slate(self):
        # Codex publishes an untouched 5h allowance as 0% with resetsAt rolled
        # to now + 300 min.  Present it as a clean slate: nominal 5h00m, no
        # elapsed marker.  The caller keeps the real resets_at untouched.
        w = Window(used_percent=0.0, resets_at=NOW + 300 * 60, nominal_minutes=300)
        text, colour, elapsed = time_budget(w, NOW)
        self.assertEqual(text, "5h00m")
        self.assertIsNone(elapsed)
        self.assertEqual(colour, model.MUTED)

    def test_unstarted_window_slightly_over_nominal_is_still_clean_slate(self):
        # live reads occasionally return resets_at - now a couple of seconds
        # over the nominal duration
        w = Window(used_percent=0.0, resets_at=NOW + 300 * 60 + 2, nominal_minutes=300)
        self.assertEqual(time_budget(w, NOW)[0], "5h00m")

    def test_unstarted_window_stays_clean_slate_through_a_heartbeat_cycle(self):
        # resets_at frozen from the last live refresh; the display clock has
        # advanced ~59s, so resets_at - now is ~4h59m01s -- still within slack.
        w = Window(used_percent=0.0, resets_at=NOW + 300 * 60, nominal_minutes=300)
        text, _c, elapsed = time_budget(w, NOW + 59)
        self.assertEqual(text, "5h00m")
        self.assertIsNone(elapsed)

    def test_zero_percent_reset_well_inside_window_is_a_real_countdown(self):
        # a genuinely active fixed window that happens to sit at exactly 0.0%:
        # its reset is meaningfully inside the nominal duration -> real state,
        # real elapsed marker, not the clean-slate nominal
        w = Window(used_percent=0.0, resets_at=NOW + 200 * 60, nominal_minutes=300)
        text, _c, elapsed = time_budget(w, NOW)
        self.assertEqual(text, "3h20m")
        self.assertIsNotNone(elapsed)
        self.assertAlmostEqual(elapsed, 100 / 3, places=3)

    def test_zero_percent_reset_just_past_slack_is_a_real_countdown(self):
        secs = 300 * 60 - model.WINDOW_START_SLACK_SECONDS - 1
        w = Window(used_percent=0.0, resets_at=NOW + secs, nominal_minutes=300)
        self.assertIsNotNone(time_budget(w, NOW)[2])

    def test_usage_above_zero_keeps_real_countdown_and_elapsed(self):
        # the instant usage rises above 0 the genuine countdown + marker resume
        w = Window(used_percent=1.0, resets_at=NOW + 300 * 60, nominal_minutes=300)
        text, _c, elapsed = time_budget(w, NOW)
        self.assertEqual(text, "5h00m")
        self.assertEqual(elapsed, 0.0)          # present (0.0), not None -> marker

    def test_near_zero_usage_is_not_treated_as_clean_slate(self):
        # a value that merely rounds to 0% in the meter is still a used window
        w = Window(used_percent=0.4, resets_at=NOW + 300 * 60, nominal_minutes=300)
        self.assertIsNotNone(time_budget(w, NOW)[2])

    def test_unknown_usage_with_rolling_reset_is_not_clean_slate(self):
        w = Window(used_percent=None, resets_at=NOW + 205 * 60, nominal_minutes=300)
        text, _c, elapsed = time_budget(w, NOW)
        self.assertEqual(text, "3h25m")        # honest raw countdown, not 5h00m
        self.assertIsNone(elapsed)

    def test_countdown_forms(self):
        self.assertEqual(time_budget(Window(10, NOW + 29 * 60, 300), NOW)[0], "29m")
        self.assertEqual(time_budget(Window(10, NOW + 3 * 3600 + 12 * 60, 300), NOW)[0], "3h12m")
        self.assertEqual(time_budget(Window(10, NOW + 34 * 3600, 10080), NOW)[0], "1d10h")
        self.assertEqual(time_budget(Window(10, NOW - 5, 300), NOW)[0], "now")

    def test_countdown_subordinate_unit_is_zero_padded(self):
        # hour/minute: 5h0m -> 5h00m, 2h3m -> 2h03m, 2h03m stays
        self.assertEqual(time_budget(Window(10, NOW + 5 * 3600, 300), NOW)[0], "5h00m")
        self.assertEqual(time_budget(Window(10, NOW + 2 * 3600 + 3 * 60, 300), NOW)[0], "2h03m")
        # day/hour: 2d1h -> 2d01h, 1d7h -> 1d07h, 6d18h stays
        self.assertEqual(time_budget(Window(10, NOW + 2 * 86400 + 3600, 10080), NOW)[0], "2d01h")
        self.assertEqual(time_budget(Window(10, NOW + 86400 + 7 * 3600, 10080), NOW)[0], "1d07h")
        self.assertEqual(time_budget(Window(10, NOW + 6 * 86400 + 18 * 3600, 10080), NOW)[0], "6d18h")

    def test_pace_colour_behind_is_green_ahead_is_red(self):
        # 50% elapsed, 20% used -> comfortably ahead of pace -> green
        behind = Window(used_percent=20.0, resets_at=NOW + 150 * 60, nominal_minutes=300)
        self.assertEqual(time_budget(behind, NOW)[1], model.GREEN)
        # 25% elapsed, 90% used -> far ahead of budget -> red
        ahead = Window(used_percent=90.0, resets_at=NOW + int(10080 * 60 * 0.75),
                       nominal_minutes=10080)
        self.assertEqual(time_budget(ahead, NOW)[1], model.RED)


class FormatResetTests(unittest.TestCase):
    def test_zero_padding_examples(self):
        self.assertEqual(model.format_reset(5 * 3600), "5h00m")
        self.assertEqual(model.format_reset(2 * 3600 + 3 * 60), "2h03m")
        self.assertEqual(model.format_reset(2 * 86400 + 3600), "2d01h")
        self.assertEqual(model.format_reset(6 * 86400 + 18 * 3600), "6d18h")
        self.assertEqual(model.format_reset(86400 + 7 * 3600), "1d07h")

    def test_minutes_only_and_now(self):
        self.assertEqual(model.format_reset(29 * 60), "29m")
        self.assertEqual(model.format_reset(0), "now")
        self.assertEqual(model.format_reset(-10), "now")

    def test_two_digit_units_are_left_alone(self):
        self.assertEqual(model.format_reset(3 * 3600 + 12 * 60), "3h12m")
        self.assertEqual(model.format_reset(34 * 3600), "1d10h")


class BarTests(unittest.TestCase):
    def test_visible_width_is_exact_and_never_below_the_sibling_floor(self):
        self.assertEqual(visible_len(bar(50.0, 50.0, 14)), 14)
        self.assertEqual(visible_len(bar(63.0, None, 14)), 14)
        self.assertEqual(visible_len(bar(None, None, 10)), 10)
        # the sibling primitive clamps to a minimum of 8
        self.assertEqual(visible_len(bar(0.0, 0.0, 6)), 8)

    def test_width_is_constant_with_and_without_the_marker(self):
        self.assertEqual(
            visible_len(bar(50.0, None, 14)), visible_len(bar(50.0, 30.0, 14))
        )
        self.assertEqual(len(_strip(bar(50.0, None, 14))), len(_strip(bar(50.0, 30.0, 14))))

    def test_percentage_is_rendered_inside_the_meter(self):
        plain = _strip(bar(63.0, None, 14))
        self.assertEqual(len(plain), 14)
        self.assertIn("63%", plain)

    def test_every_cell_has_a_background_colour_so_the_meter_is_continuous(self):
        # one background sequence per visible cell -> no gap cells. Fills are
        # 24-bit (48;2;...); the empty background is 256-colour (48;5;...).
        rendered = bar(50.0, 30.0, 14)
        self.assertEqual(rendered.count("\x1b[48;"), 14)
        self.assertEqual(rendered.count("\x1b[48;2;"), 7)   # 50% of 14 filled
        self.assertEqual(rendered.count("\x1b[48;5;"), 7)

    def test_marker_overlays_exactly_one_cell(self):
        no = _strip(bar(10.0, None, 14))
        yes = _strip(bar(10.0, 10.0, 14))          # elapsed 10% -> index 1
        self.assertEqual(yes.count("│"), 1)
        diffs = [i for i in range(len(no)) if no[i] != yes[i]]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(yes[diffs[0]], "│")

    def test_marker_overwrites_a_percentage_digit_and_stays_visible(self):
        # width 14, "50%" centres over indices 5,6,7. elapsed ~38.5% -> the
        # marker's target cell is index 5, the "5" digit. It must still appear.
        plain = _strip(bar(50.0, 38.5, 14))
        self.assertEqual(len(plain), 14)
        self.assertEqual(plain.count("│"), 1)
        self.assertEqual(plain.index("│"), 5)
        self.assertEqual(plain[6:8], "0%")        # rest of the label survives
        self.assertNotIn("5│", plain)             # the "5" was overwritten, not shifted

    def test_marker_visible_in_representative_5h_and_weekly_windows(self):
        five = Window(50.0, NOW + 150 * 60, 300)                    # ~50% elapsed
        weekly = Window(40.0, NOW + int(10080 * 60 * 0.5), 10080)   # ~50% elapsed
        for window in (five, weekly):
            _text, _colour, elapsed = time_budget(window, NOW)
            self.assertIsNotNone(elapsed)
            rendered = bar(window.used_percent, elapsed, 14)
            self.assertIn("│", _strip(rendered))
            self.assertEqual(visible_len(rendered), 14)

    def test_marker_placed_by_elapsed_not_by_usage(self):
        plain = _strip(bar(10.0, 90.0, 14))       # tiny usage, near-full elapsed
        self.assertEqual(plain.index("│"), round(0.9 * 13))

    def test_marker_absent_when_elapsed_unknown(self):
        self.assertNotIn("│", _strip(bar(40.0, None, 14)))
        self.assertNotIn("│", _strip(bar(None, None, 14, label="n/a")))

    def test_capacity_colours_are_the_battery_status_tui_palette(self):
        # fills lifted straight from battery-status-tui's BATTERY_COLOR_STOPS
        self.assertIn("48;2;20;105;50", bar(50.0, None, 14))    # green  < 70% used
        self.assertIn("48;2;175;110;25", bar(80.0, None, 14))   # amber 70-90% used
        self.assertIn("48;2;155;35;30", bar(95.0, None, 14))    # red    >= 90% used
        # empty background is a plain dark xterm 236
        self.assertIn("48;5;236", bar(50.0, None, 14))
        self.assertNotIn("48;5;238", bar(50.0, None, 14))

    def test_meter_text_is_a_single_light_colour_on_fill_and_empty(self):
        rendered = bar(50.0, 30.0, 14)
        self.assertIn("38;5;252", rendered)
        self.assertNotIn("38;5;232", rendered)                  # old dark on-fill gone

    def test_ansi_restyle_does_not_change_visible_width(self):
        for pct in (0.0, 45.0, 63.0, 80.0, 95.0, None):
            self.assertEqual(visible_len(bar(pct, 40.0, 14)), 14)
            self.assertEqual(visible_len(bar(pct, None, 10)), 10)


def _strip(text):
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


if __name__ == "__main__":
    unittest.main()
