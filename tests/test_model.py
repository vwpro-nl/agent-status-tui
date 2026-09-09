import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentstatus import model
from agentstatus.model import Window, age_text, bar, place_label, time_budget
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

    def test_label_steps_aside_when_the_marker_reaches_the_centred_label(self):
        # width 14, "50%" centres over indices 5,6,7. elapsed ~38.5% -> the
        # marker's real cell is index 5, on the "5" digit. The marker holds its
        # cell; the whole label moves clear instead of losing a digit.
        plain = _strip(bar(50.0, 38.5, 14))
        self.assertEqual(len(plain), 14)
        self.assertEqual(plain.count("│"), 1)
        self.assertEqual(plain.index("│"), 5)      # marker exactly where 38.5% puts it
        self.assertEqual(round(0.385 * 13), 5)     # ... the proportional cell, unmoved
        self.assertIn("50%", plain)                # complete label still present
        self.assertNotIn("5│", plain)              # not shifted onto the marker
        self.assertNotIn("│%", plain)              # marker did not eat a digit
        # displaced to the right of the marker with one blank separating cell
        self.assertEqual(plain[5:10], "│ 50%")

    def test_marker_cell_is_identical_whatever_the_label_does(self):
        # the marker index must not depend on where the label ends up
        for elapsed in (0.0, 12.0, 38.5, 46.0, 50.0, 54.0, 88.0, 100.0):
            with_label = _strip(bar(50.0, elapsed, 14))
            blank_label = _strip(bar(None, elapsed, 14, label=""))
            self.assertEqual(with_label.index("│"), blank_label.index("│"))
            self.assertEqual(with_label.index("│"), round(elapsed / 100 * 13))

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


class PlaceLabelTests(unittest.TestCase):
    """The deterministic percentage-label placement algorithm.

    Invariant under test everywhere: ``marker_index`` is an input, never an
    output -- ``place_label`` only ever positions (or drops) the label.
    """

    def test_marker_none_is_always_centred(self):
        self.assertEqual(place_label(16, "42%", None), "42%".center(16))
        self.assertEqual(place_label(22, "100%", None), "100%".center(22))

    def test_marker_far_left_keeps_label_centred(self):
        row = place_label(16, "42%", 1)
        self.assertEqual(row, "42%".center(16))

    def test_marker_far_right_keeps_label_centred(self):
        row = place_label(16, "42%", 15)
        self.assertEqual(row, "42%".center(16))

    def test_marker_one_clear_cell_from_label_still_centred(self):
        # centred "42%" over width 16 starts at 6; a marker at 4 leaves cell 5
        # blank between it and the label -> no displacement.
        self.assertEqual(place_label(16, "42%", 4), "42%".center(16))

    def test_marker_immediately_left_of_label_moves_label_right(self):
        # marker at 5 (touching the centred label at 6) -> label to the right,
        # one blank separating cell at index 6, marker cell 5 untouched here.
        row = place_label(16, "42%", 5)
        self.assertEqual(row.index("42%"), 7)
        self.assertEqual(row[5], " ")          # place_label never writes the marker
        self.assertEqual(row[6], " ")          # separating blank

    def test_marker_immediately_right_of_label_moves_label_left(self):
        # centred "42%" spans 6..8; marker at 9 -> label shifts left of it.
        row = place_label(16, "42%", 9)
        self.assertEqual(row.index("42%"), 5)
        self.assertEqual(row[8], " ")          # separating blank
        self.assertEqual(len(row), 16)

    def test_marker_inside_centred_span_moves_label_to_a_side(self):
        row = place_label(16, "42%", 7)        # dead on the middle digit
        start = row.index("42%")
        self.assertTrue(start >= 9 or start + 3 <= 6)   # clear of marker + a gap
        self.assertNotIn("4", row[:7])                  # nothing left on the marker

    def test_marker_at_centre_still_shows_the_whole_label(self):
        for marker in (7, 8):
            row = place_label(16, "42%", marker)
            self.assertIn("42%", row)
            self.assertEqual(len(row), 16)
            self.assertNotEqual(row[marker], "4")

    def test_label_returns_to_centre_as_soon_as_the_marker_clears_it(self):
        centred = "42%".center(16)
        # sweep the marker away from the label on the left
        for marker in range(0, 5):
            self.assertEqual(place_label(16, "42%", marker), centred)
        # ... and on the right (centred label ends at 8, +1 gap -> clears at 10)
        for marker in range(10, 16):
            self.assertEqual(place_label(16, "42%", marker), centred)

    def test_hundred_percent_label_is_handled(self):
        for width in (16, 17, 22, 23):
            for marker in range(width):
                row = place_label(width, "100%", marker)
                self.assertEqual(len(row), width)
                if row.strip():
                    self.assertIn("100%", row)
                    # label never sits on the marker cell
                    self.assertNotIn(row[marker], set("100%"))

    def test_one_and_two_digit_labels_fit_across_compact_and_wide_bars(self):
        # compact 62-col bars are 16 and 17; a comfortably wide layout is ~22.
        for width in (16, 17, 22, 23):
            for label in ("5%", "42%", "100%"):
                dropped = 0
                for marker in range(width):
                    row = place_label(width, label, marker)
                    self.assertEqual(len(row), width)
                    if not row.strip():
                        dropped += 1
                    else:
                        self.assertIn(label, row)
                # a two-digit label is never dropped at a real dashboard width
                if label != "100%":
                    self.assertEqual(dropped, 0, (width, label))

    def test_smallest_bar_width_still_places_two_digit_labels(self):
        placed = [bool(place_label(8, "42%", m).strip()) for m in range(8)]
        self.assertTrue(all(placed))

    def test_label_dropped_only_as_a_last_resort(self):
        # width 8, "100%" (4 cells) + a marker near the centre: no side can hold
        # the label without leaving the meter -> it is dropped, marker survives.
        row = place_label(8, "100%", 4)
        self.assertEqual(row, " " * 8)

    def test_marker_index_is_never_changed_for_the_label(self):
        # For every width/label/marker the marker cell that bar() paints is
        # exactly round(elapsed proportion) and does not depend on the label.
        for width in (8, 12, 16, 17, 22, 23, 30):
            for elapsed in range(0, 101, 7):
                marker = round(elapsed / 100 * (width - 1))
                for label in ("5%", "42%", "100%", "n/a", "--"):
                    painted = _strip(
                        bar(50.0, elapsed, width, label=label)
                    )
                    self.assertEqual(painted.index("│"), marker, (width, elapsed, label))


def _strip(text):
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


if __name__ == "__main__":
    unittest.main()
