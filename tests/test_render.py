import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentstatus.model import (
    BAR_EMPTY_BG,
    BOLD,
    CYAN,
    GREEN,
    MUTED,
    RESET,
    AgentStatus,
    Window,
)
from agentstatus.render import (
    COMPACT_GEOMETRY,
    COMPACT_MIN,
    FULL_GEOMETRY,
    _clock,
    _allocated_geometry,
    _footer,
    _is_unsupported,
    _window_block,
    render,
    render_frame,
    visible_len,
)

NOW = 1_000_000_000.0
STRIP = re.compile(r"\x1b\[[0-9;]*m")


def plain(text):
    return STRIP.sub("", text)


def sample_agents():
    return [
        AgentStatus("codex", "CODEX",
                    Window(34.0, NOW + 29 * 60, 300),
                    Window(46.0, NOW + 34 * 3600, 10080),
                    "live", None, NOW - 5, "ok"),
        AgentStatus("claude", "CLAUDE",
                    Window(63.0, NOW + 18 * 60, 300),
                    Window(62.0, NOW + 52 * 3600, 10080),
                    "cache", 660.0, NOW - 200, "ok"),
        AgentStatus("grok", "GROK", None, None, "activity", 22.0, NOW - 22, "no-capacity-data"),
    ]


def table_geometry(width=80):
    base = FULL_GEOMETRY if width >= 72 else COMPACT_GEOMETRY
    return _allocated_geometry(width, base)


class NormalWidthStructureTests(unittest.TestCase):
    def setUp(self):
        self.out = render(sample_agents(), NOW, 60, NOW + 28, 80)
        self.lines = plain(self.out).split("\n")

    def test_line_count_is_title_header_rows_footer(self):
        # 1 title + 1 header + 3 agent rows + 1 footer
        self.assertEqual(len(self.lines), 6)

    def test_no_blank_lines(self):
        for line in self.lines:
            self.assertNotEqual(line.strip(), "")

    def test_exact_header_row(self):
        agent_w, first_w, second_w, reset_w, gap, block_gap = table_geometry()
        self.assertEqual(
            self.lines[1],
            "agent".ljust(agent_w) + " "
            + "5h".center(first_w) + " " * gap + "reset".ljust(reset_w)
            + " " * block_gap
            + "week".center(second_w) + " " * gap + "reset".ljust(reset_w)
            + " " * block_gap + "data",
        )

    def test_one_row_per_agent_in_order(self):
        self.assertTrue(self.lines[2].startswith("CODEX "))
        self.assertTrue(self.lines[3].startswith("CLAUDE "))
        self.assertTrue(self.lines[4].startswith("GROK "))

    def test_5h_and_week_headers_are_centred_over_their_meter_columns(self):
        header = self.lines[1]
        agent_w, first_w, second_w, reset_w, gap, block_gap = table_geometry()
        five_start = agent_w + 1
        week_start = five_start + first_w + gap + reset_w + block_gap
        self.assertEqual(header[five_start:five_start + first_w], "5h".center(first_w))
        self.assertEqual(header[week_start:week_start + second_w], "week".center(second_w))
        # the meter columns in the rows occupy exactly those same spans
        row = plain(self.lines[2])
        self.assertIn("34%", row[five_start:five_start + first_w])
        self.assertIn("46%", row[week_start:week_start + second_w])

    def test_percentage_is_inside_the_meter_not_a_separate_column(self):
        row = plain(self.lines[2])                       # CODEX
        agent_w, first_w, _second_w, _reset_w, gap, _block_gap = table_geometry()
        five_meter = row[agent_w + 1:agent_w + 1 + first_w]
        self.assertRegex(five_meter, r"\d+%")            # % lives in the meter
        # right after the meter comes the gap then the reset text, no "NN%"
        after = row[agent_w + 1 + first_w:agent_w + 1 + first_w + gap]
        self.assertEqual(after, " " * gap)
        self.assertEqual(row.count("%"), 2)             # exactly one per meter

    def test_marker_does_not_widen_the_meter(self):
        # every row is the same width its columns imply, marker or not
        for row in self.lines[2:4]:                      # CODEX, CLAUDE (have markers)
            self.assertIn("│", row)
            self.assertLessEqual(visible_len(row), 80)
        self.assertEqual(visible_len(self.lines[2]), 80)  # four-cell LIVE fills data

    def test_title_is_centred(self):
        title = self.lines[0]
        self.assertIn("AGENT STATUS", title)
        body_w = max(visible_len(l) for l in self.lines[1:5])
        lead = len(title) - len(title.lstrip(" "))
        self.assertEqual(lead, (body_w - len("AGENT STATUS")) // 2)

    def test_footer_left_edge_is_the_5h_meter_column(self):
        footer = self.lines[5]
        stripped = footer.strip()
        # wording unchanged: date/time first, then ` · (<interval>) refresh in Ns`
        self.assertRegex(
            stripped,
            r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} "
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} "
            r"\d{2}:\d{2} · \(1m\) refresh in 28s$",
        )
        self.assertNotIn("Ctrl-C", footer)
        self.assertNotIn("quit", footer)
        self.assertNotIn("snapshot", footer)
        # left edge sits exactly under the 5h meter's left column, not centred
        agent_w, bar_w = 10, 14
        five_start = agent_w + 1
        self.assertEqual(len(footer) - len(footer.lstrip(" ")), five_start)
        self.assertIn("34%", plain(self.lines[2])[five_start:five_start + bar_w])
        self.assertEqual(footer, footer.rstrip(" "))          # no trailing pad

    def test_data_column_live_cache_activity(self):
        self.assertTrue(self.lines[2].rstrip().endswith("LIVE"))
        self.assertTrue(self.lines[3].rstrip().endswith("11m"))    # 660s -> 11m
        self.assertTrue(self.lines[4].rstrip().endswith("22s"))

    def test_missing_grok_windows_render_as_double_dash_not_zero(self):
        row = self.lines[4]
        self.assertIn("--", row)
        self.assertNotIn("0%", row)
        self.assertNotIn("█", row)

    def test_elapsed_marker_present_in_every_available_bar(self):
        for row in (self.lines[2], self.lines[3]):
            # two bars per row, each must carry a marker
            self.assertEqual(row.count("│"), 2)
        self.assertEqual(self.lines[4].count("│"), 0)   # grok has no bars

    def test_no_emoji_only_box_drawing_and_ascii(self):
        allowed_non_ascii = set("█░│·…")
        for ch in plain(self.out):
            if ord(ch) > 127:
                self.assertIn(ch, allowed_non_ascii)

    def test_no_decorative_rule_characters(self):
        self.assertNotIn("=", self.out)
        self.assertNotIn("---", plain(self.out).replace("--", ""))  # only the honest "--"


class PaletteHarmonisationTests(unittest.TestCase):
    """The title/footer/meter colours reuse battery-status-tui's palette."""

    def setUp(self):
        self.out = render(sample_agents(), NOW, 60, NOW + 28, 80)
        self.raw = self.out.split("\n")
        self.lines = plain(self.out).split("\n")

    def test_title_uses_battery_bold_cyan_heading_style(self):
        self.assertTrue(self.raw[0].endswith(RESET))
        self.assertIn(BOLD + CYAN + "AGENT STATUS" + RESET, self.raw[0])
        self.assertEqual(self.lines[0].strip(), "AGENT STATUS")   # unchanged visibly

    def test_footer_uses_battery_muted_secondary_colour(self):
        footer = self.raw[-1]
        self.assertIn(MUTED, footer)
        self.assertRegex(
            plain(footer).strip(),
            r"^\w{3} \d{1,2} \w{3} \d{4} \d{2}:\d{2} · \(1m\) refresh in 28s$",
        )
        self.assertNotIn("Ctrl-C", plain(footer))                 # quit hint removed
        self.assertNotIn(CYAN, footer)                            # not made a heading

    def test_header_row_is_entirely_muted_matching_the_footer(self):
        header = self.raw[1]
        self.assertTrue(header.startswith(MUTED))
        self.assertTrue(header.endswith(RESET))
        self.assertEqual(header.count("\x1b["), 2)                # one MUTED span only
        self.assertNotIn(CYAN, header)
        # Positioning and spacing fill the selected content width exactly.
        self.assertEqual(visible_len(header), 80)
        self.assertTrue(plain(header).endswith("data"))

    def test_meter_fills_are_battery_stop_colours_not_the_old_pastels(self):
        codex = self.raw[2]
        self.assertIn("\x1b[48;2;20;105;50m", codex)              # CODEX 5h 34% -> green
        for gone in ("48;5;114", "48;5;221", "48;5;203"):        # old pastel fills
            self.assertNotIn(gone, self.out)

    def test_empty_meter_background_is_dark_xterm_236(self):
        self.assertIn("\x1b[48;5;236m", self.out)
        self.assertNotIn("\x1b[48;5;238m", self.out)

    def test_restyle_keeps_every_line_within_the_pane(self):
        for line in self.raw:
            self.assertLessEqual(visible_len(line), 80)
        # visible content identical to a plain re-render of the same frame
        self.assertEqual(len(self.lines), 6)
        self.assertNotIn("", [l.strip() for l in self.lines])     # still no blanks


class HeartbeatTests(unittest.TestCase):
    def test_repaint_does_not_change_provider_data_between_refreshes(self):
        # Same frozen AgentStatus list, two paints 3s apart.  The per-agent
        # source-age ("data" column) and the usage percentages must be
        # identical -- only the clock-derived countdowns move.
        agents = sample_agents()
        rows_a = plain(render(agents, NOW, 60, NOW + 28, 80)).split("\n")[2:5]
        rows_b = plain(render(agents, NOW + 3, 60, NOW + 28, 80)).split("\n")[2:5]

        def data_and_pct(row):
            return row.rstrip().split()[-1], re.findall(r"\d+%", row)

        for ra, rb in zip(rows_a, rows_b):
            self.assertEqual(data_and_pct(ra), data_and_pct(rb))

    def test_footer_countdown_does_tick_between_heartbeats(self):
        agents = sample_agents()
        a = plain(render(agents, NOW, 60, NOW + 28, 80))
        b = plain(render(agents, NOW + 3, 60, NOW + 28, 80))
        self.assertIn("in 28s", a)
        self.assertIn("in 25s", b)

    def test_render_never_touches_provider_adapters(self):
        # render() references nothing that could poll a provider.
        self.assertNotIn("poll", render.__code__.co_names)
        self.assertNotIn("collect", render.__code__.co_names)
        out = render(sample_agents(), NOW, 60, NOW + 10, 80)
        self.assertIn("AGENT STATUS", out)


class NarrowWidthTests(unittest.TestCase):
    def test_compact_tier_keeps_all_columns_no_wrap(self):
        out = render(sample_agents(), NOW, 60, NOW + 28, 60)
        lines = plain(out).split("\n")
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertLessEqual(visible_len(line), 60)
        self.assertIn("reset", lines[1])       # resets still present

    def test_text_tier_drops_bars_but_keeps_one_row_per_agent(self):
        out = render(sample_agents(), NOW, 60, NOW + 28, 34)
        lines = plain(out).split("\n")
        self.assertEqual(len(lines), 6)
        self.assertNotIn("█", out)
        self.assertTrue(lines[2].startswith("CODEX"))
        self.assertIn("34%/46%", lines[2])
        self.assertIn("--/--", lines[4])
        for line in lines:
            self.assertLessEqual(visible_len(line), 34)

    def test_never_wraps_rows_at_any_width(self):
        for width in range(20, 130, 3):
            out = render(sample_agents(), NOW, 60, NOW + 28, width)
            lines = out.split("\n")
            self.assertEqual(len(lines), 6, f"width {width} changed line count")
            for line in lines:
                self.assertLessEqual(visible_len(line), width)


class TerminalFrameTests(unittest.TestCase):
    def test_continuous_bar_allocation_at_62_terminal_columns(self):
        geometry = _allocated_geometry(60, COMPACT_GEOMETRY)
        self.assertEqual(geometry[1:3], (16, 17))
        lines = render_frame(sample_agents(), NOW, 60, NOW + 28, 62, 7).splitlines()
        codex = plain(lines[2])
        self.assertTrue(codex.startswith(" CODEX"))
        self.assertTrue(codex.endswith("LIVE "))

    def test_bars_grow_at_a_wider_compact_width(self):
        self.assertEqual(_allocated_geometry(68, COMPACT_GEOMETRY)[1:3], (20, 21))
        self.assertEqual(visible_len(render(sample_agents(), NOW, 60, NOW + 28, 68).splitlines()[1]), 68)

    def test_minimum_graphical_width_and_text_fallback(self):
        self.assertEqual(COMPACT_MIN, 35)
        self.assertEqual(_allocated_geometry(COMPACT_MIN, COMPACT_GEOMETRY)[1:3], (4, 4))
        graphical = plain(render(sample_agents(), NOW, 60, NOW + 28, COMPACT_MIN))
        text_only = plain(render(sample_agents(), NOW, 60, NOW + 28, COMPACT_MIN - 1))
        self.assertIn("reset", graphical.splitlines()[1])
        self.assertIn("5h", graphical.splitlines()[1])
        self.assertIn("5h", text_only.splitlines()[1])
        self.assertIn("/wk", text_only.splitlines()[1])

    def test_exact_minimum_62_by_7_keeps_all_visible_rows_and_margins(self):
        lines = render_frame(sample_agents(), NOW, 60, NOW + 28, 62, 7).splitlines()
        self.assertEqual(len(lines), 6)
        self.assertIn("AGENT STATUS", plain(lines[0]))
        self.assertIn("refresh", plain(lines[-1]))
        for line in lines:
            text = plain(line)
            self.assertEqual(visible_len(line), 62)
            self.assertEqual((text[0], text[-1]), (" ", " "))

    def test_real_terminal_width_has_exact_one_cell_side_margins(self):
        for width in (34, 62, 81, 110):
            lines = render_frame(sample_agents(), NOW, 60, NOW + 28, width, 10).splitlines()
            self.assertTrue(lines)
            for line in lines:
                text = plain(line)
                self.assertEqual(visible_len(line), width)
                self.assertEqual(text[0], " ")
                self.assertEqual(text[-1], " ")

    def test_short_terminal_degrades_without_exceeding_height(self):
        for height in range(1, 6):
            lines = render_frame(sample_agents(), NOW, 60, NOW + 28, 62, height).splitlines()
            self.assertLessEqual(len(lines), height)
            self.assertTrue(all(visible_len(line) == 62 for line in lines))


class MarkerVisibilityInRenderTests(unittest.TestCase):
    def test_marker_stays_visible_when_it_lands_on_a_percentage_digit(self):
        # elapsed ~46% -> marker cell is index 6 of a 14-cell meter, which is
        # the middle character of the centred "63%" label.
        agents = [
            AgentStatus("codex", "CODEX",
                        Window(63.0, NOW + 9720, 300),   # ~46% elapsed
                        None, "live", None, NOW - 5, "ok"),
        ]
        text = plain(render(agents, NOW, 60, NOW + 28, 80))
        codex = next(l for l in text.split("\n") if l.startswith("CODEX"))
        _agent_w, first_w, _second_w, _reset_w, _gap, _block_gap = table_geometry()
        five_meter = codex[11:11 + first_w]
        self.assertIn("│", five_meter)
        self.assertIn("6│%", five_meter)
        self.assertEqual(len(five_meter), first_w)

    def test_marker_absent_for_grok_na_placeholder(self):
        text = plain(render(sample_agents(), NOW, 60, NOW + 28, 80))
        grok = next(l for l in text.split("\n") if l.startswith("GROK"))
        self.assertNotIn("│", grok)


class GrokUnsupportedFiveHourTests(unittest.TestCase):
    """The Grok 5h window is *known* to have no quota source. It renders an
    aligned `n/a` / green `5h00m` placeholder -- distinct from the `--` used
    for data that is merely unavailable."""

    def _grok_line(self, width=80):
        out = render(sample_agents(), NOW, 60, NOW + 28, width)
        raw = next(l for l in out.split("\n") if "GROK" in l)
        return raw, plain(raw)

    def test_only_grok_five_hour_is_flagged_unsupported(self):
        self.assertTrue(_is_unsupported("grok", "five_hour"))
        self.assertFalse(_is_unsupported("grok", "weekly"))
        self.assertFalse(_is_unsupported("codex", "five_hour"))
        self.assertFalse(_is_unsupported("claude", "five_hour"))

    def test_grok_five_hour_block_is_dark_meter_with_centred_na_and_green_reset(self):
        block = _window_block(None, NOW, 14, 6, 2, unsupported=True)
        self.assertEqual(plain(block), "n/a".center(14) + "  " + "5h00m".ljust(6))
        self.assertIn(BAR_EMPTY_BG, block)          # normal dark meter background
        self.assertNotIn("48;5;114", block)         # no capacity fill colour
        self.assertNotIn("48;5;221", block)
        self.assertNotIn("│", block)                # no fabricated elapsed marker
        self.assertIn(GREEN + "5h00m", block)       # green / healthy reset colour

    def test_grok_row_shows_na_and_padded_5h00m_in_a_full_render(self):
        raw, text = self._grok_line(80)
        _agent_w, first_w, _second_w, _reset_w, _gap, _block_gap = table_geometry()
        five_meter = text[11:11 + first_w]
        self.assertEqual(five_meter, "n/a".center(first_w))
        self.assertIn("5h00m", text)
        self.assertNotIn("5h0m ", text)             # not the unpadded form
        self.assertIn(GREEN + "5h00m", raw)
        self.assertNotIn("0%", text)                # never a fabricated percentage

    def test_grok_row_stays_exactly_the_expected_width(self):
        _raw, text = self._grok_line(80)
        self.assertEqual(visible_len(text), 79)  # three-cell age in four-cell data field

    def test_grok_weekly_is_unchanged_and_still_double_dash_when_missing(self):
        _raw, text = self._grok_line(80)
        agent_w, first_w, second_w, reset_w, gap, block_gap = table_geometry()
        week_start = agent_w + 1 + first_w + gap + reset_w + block_gap
        week_meter = text[week_start:week_start + second_w]
        self.assertEqual(week_meter.strip(), "--")
        self.assertNotIn("n/a", week_meter)

    def test_other_agents_missing_window_stays_unknown_double_dash_not_na(self):
        agents = [
            AgentStatus("codex", "CODEX", None,
                        Window(46.0, NOW + 34 * 3600, 10080),
                        "live", None, NOW - 5, "ok"),
        ]
        text = plain(render(agents, NOW, 60, NOW + 28, 80))
        codex = next(l for l in text.split("\n") if l.startswith("CODEX"))
        five_meter = codex[11:25]
        self.assertEqual(five_meter.strip(), "--")
        self.assertNotIn("n/a", codex)
        self.assertNotIn("5h00m", codex)

    def test_compact_tier_also_places_the_na_placeholder(self):
        out = plain(render(sample_agents(), NOW, 60, NOW + 28, 60))
        grok = next(l for l in out.split("\n") if l.startswith("GROK"))
        self.assertIn("n/a", grok)
        self.assertIn("5h00m", grok)


class EmptyAndErrorTests(unittest.TestCase):
    def test_no_agents_still_renders_title_header_footer(self):
        out = plain(render([], NOW, 60, NOW + 28, 80))
        lines = out.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertIn("AGENT STATUS", lines[0])
        self.assertIn("refresh", lines[2])

    def test_error_row_shows_ERR_without_crashing(self):
        agents = [AgentStatus("codex", "CODEX", None, None, "none", None, NOW - 5,
                              "error", "boom")]
        out = plain(render(agents, NOW, 60, NOW + 28, 80))
        self.assertIn("ERR", out)
        # title + header + 1 row + footer
        self.assertEqual(len(out.split("\n")), 4)
        self.assertTrue(out.split("\n")[2].startswith("CODEX "))


class UnstartedWindowRenderTests(unittest.TestCase):
    """A provider (Codex) reporting an untouched 5h allowance as 0% with a
    rolling resetsAt renders as a clean slate: 0%, nominal 5h00m, no marker.
    The renderer never special-cases the provider -- this all comes from
    time_budget()."""

    def _codex_row(self, now, five, weekly=None, width=80):
        agents = [AgentStatus("codex", "CODEX", five, weekly,
                              "live", None, NOW - 5, "ok")]
        text = plain(render(agents, now, 60, now + 28, width))
        return next(l for l in text.split("\n") if l.startswith("CODEX"))

    def test_zero_percent_rolling_reset_shows_5h00m_and_no_marker(self):
        row = self._codex_row(NOW, Window(0.0, NOW + 300 * 60, 300))
        five_meter = row[11:25]
        self.assertIn("0%", five_meter)
        self.assertNotIn("│", five_meter)
        self.assertIn("5h00m", row)

    def test_clean_slate_survives_a_heartbeat_advance(self):
        # frozen reset from the last refresh, display clock moved on ~59s
        row = self._codex_row(NOW + 59, Window(0.0, NOW + 300 * 60, 300))
        self.assertNotIn("│", row[11:25])
        self.assertIn("5h00m", row)

    def test_active_codex_usage_gets_a_real_countdown_and_marker(self):
        row = self._codex_row(NOW, Window(20.0, NOW + 150 * 60, 300))
        five_meter = row[11:25]
        self.assertIn("│", five_meter)          # elapsed marker back
        self.assertIn("2h30m", row)             # real countdown

    def test_zero_percent_fixed_window_inside_duration_keeps_its_countdown(self):
        row = self._codex_row(NOW, Window(0.0, NOW + 120 * 60, 300))
        self.assertIn("│", row[11:25])
        self.assertIn("2h00m", row)

    def test_weekly_window_is_unaffected_by_clean_slate(self):
        row = self._codex_row(
            NOW,
            Window(0.0, NOW + 300 * 60, 300),
            Window(48.0, NOW + 34 * 3600, 10080),
        )
        agent_w, first_w, second_w, reset_w, gap, block_gap = table_geometry()
        week_start = agent_w + 1 + first_w + gap + reset_w + block_gap
        week_meter = row[week_start:week_start + second_w]
        self.assertIn("│", week_meter)          # weekly keeps its real marker
        self.assertIn("48%", week_meter)


class FooterLayoutTests(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def _footer_line(self, now, width=80, interval=60):
        out = plain(render(sample_agents(), now, interval, now + 28, width))
        return out.split("\n")[-1]

    # 1_600_000_000 -> 2020-09-13 12:26:40 UTC (Sunday, 2-digit day)
    # 1_599_600_000 -> 2020-09-08 21:20:00 UTC (Tuesday, 1-digit day)

    def test_exact_full_width_footer_wording(self):
        self.assertEqual(
            self._footer_line(1_600_000_000).strip(),
            "Sun 13 Sep 2020 12:26 · (1m) refresh in 28s",
        )

    def test_single_digit_day_has_no_leading_zero(self):
        self.assertEqual(
            self._footer_line(1_599_600_000).strip(),
            "Tue 8 Sep 2020 21:20 · (1m) refresh in 28s",
        )

    def test_configured_interval_is_reflected_not_hard_coded(self):
        self.assertRegex(self._footer_line(1_600_000_000, interval=30).strip(),
                         r"· \(30s\) refresh in 28s$")
        self.assertRegex(self._footer_line(1_600_000_000, interval=120).strip(),
                         r"· \(2m\) refresh in 28s$")
        self.assertRegex(self._footer_line(1_600_000_000, interval=45).strip(),
                         r"· \(45s\) refresh in 28s$")

    def test_full_footer_starts_at_the_5h_meter_column(self):
        lines = plain(render(sample_agents(), 1_600_000_000, 60,
                             1_600_000_028, 80)).split("\n")
        footer = lines[-1]
        agent_w, bar_w = 10, 14
        five_start = agent_w + 1
        self.assertEqual(len(footer) - len(footer.lstrip(" ")), five_start)
        self.assertIn("34%", lines[2][five_start:five_start + bar_w])
        self.assertEqual(footer.strip(),
                         "Sun 13 Sep 2020 12:26 · (1m) refresh in 28s")

    def test_full_footer_left_edge_fixed_when_countdown_shrinks(self):
        # 10s -> 9s: the 'S' of 'Sun' stays in the same column; only the
        # right-hand end loses a character.
        a = plain(render(sample_agents(), 1_600_000_000, 60,
                         1_600_000_010, 80)).split("\n")[-1]
        b = plain(render(sample_agents(), 1_600_000_000, 60,
                         1_600_000_009, 80)).split("\n")[-1]
        lead_a = len(a) - len(a.lstrip(" "))
        lead_b = len(b) - len(b.lstrip(" "))
        self.assertEqual(lead_a, lead_b)
        self.assertEqual(lead_a, 11)                          # agent_w + 1
        self.assertEqual(a[: a.index("in ")], b[: b.index("in ")])
        self.assertTrue(a.strip().endswith("refresh in 10s"))
        self.assertTrue(b.strip().endswith("refresh in 9s"))

    def test_footer_helper_fixed_start_column(self):
        clock = "Sun 13 Sep 2020 12:26"
        out = _footer(clock, "1m", 7, 63, start_col=11, max_w=80)
        self.assertTrue(out.startswith(" " * 11 + clock))
        self.assertEqual(len(out) - len(out.lstrip(" ")), 11)
        # left edge unchanged when the countdown is one digit shorter
        shorter = _footer(clock, "1m", 9, 63, start_col=11, max_w=80)
        self.assertEqual(len(shorter) - len(shorter.lstrip(" ")), 11)
        # cannot fit at the fixed column -> centred fallback, not column 11
        tight = _footer(clock, "1m", 7, 30, start_col=11, max_w=30)
        self.assertNotEqual(len(tight) - len(tight.lstrip(" ")), 11)

    def test_ctrl_c_quit_is_absent(self):
        footer = self._footer_line(1_600_000_000)
        self.assertNotIn("Ctrl-C", footer)
        self.assertNotIn("quit", footer)

    def test_locale_independent_english_names(self):
        import locale

        # Always holds: _clock is built from fixed English tables, not strftime.
        self.assertEqual(_clock(1_600_000_000), "Sun 13 Sep 2020 12:26")
        self.assertNotIn("strftime", _clock.__code__.co_names)

        # And still holds under a non-English LC_TIME where one is installed.
        for cand in ("de_DE.UTF-8", "de_DE.utf8", "fr_FR.UTF-8", "fr_FR.utf8"):
            try:
                locale.setlocale(locale.LC_TIME, cand)
            except locale.Error:
                continue
            self.addCleanup(locale.setlocale, locale.LC_TIME, "C")
            self.assertEqual(_clock(1_600_000_000), "Sun 13 Sep 2020 12:26")
            break

    def test_muted_colour_retained(self):
        from agentstatus.model import MUTED

        raw = render(sample_agents(), 1_600_000_000, 60, 1_600_000_028, 80).split("\n")
        self.assertIn(MUTED, raw[-1])

    def test_narrow_width_degrades_cleanly_never_wraps_keeps_table(self):
        # width where the full footer cannot fit: date/time is dropped first,
        # the live countdown is kept, the line never wraps and the table is
        # untouched.
        for w in (30, COMPACT_MIN - 1):
            lines = plain(render(sample_agents(), 1_600_000_000, 60,
                                 1_600_000_028, w)).split("\n")
            self.assertEqual(len(lines), 6)                 # fixed line count
            footer = lines[-1]
            self.assertLessEqual(visible_len(footer), w)
            self.assertNotIn("2020", footer)               # date/time dropped
            self.assertIn("refresh in 28s", footer)         # countdown kept
            self.assertTrue(lines[2].startswith("CODEX"))   # table geometry intact
            self.assertIn("34%/46%", lines[2])

    def test_footer_helper_degrades_in_order(self):
        clock = "Sun 13 Sep 2020 12:26"
        full = f"{clock} · (1m) refresh in 5s"
        # fits -> full line, centred
        self.assertEqual(_footer(clock, "1m", 5, 60).strip(), full)
        # no room for the date/time -> parenthesised interval + countdown
        self.assertEqual(_footer(clock, "1m", 5, 25).strip(), "(1m) refresh in 5s")
        # no room for that either -> bare countdown
        self.assertEqual(_footer(clock, "1m", 5, 15).strip(), "refresh in 5s")


if __name__ == "__main__":
    unittest.main()
