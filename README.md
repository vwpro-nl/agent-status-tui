# agent-status-tui

One compact, read-only terminal dashboard for the coding agents installed on
this machine. It replaces the separate per-agent status panes with a single
table: automatic detection, one row per agent, ordered by recent activity.

```
                     AGENT STATUS
agent      5h               reset   week             reset   data
CODEX      ███░░░░░│░  34%   29m     █████░░│░░  46%   1d10h   LIVE
CLAUDE     ██████░░│░  63%   18m     ██████│░░░  62%   2d4h    11m
GROK       --               --      --               --       22s
           Sun 6 Sep 2026 05:40 · (1m) refresh in 28s
```

Standard library only. The only network call is the Grok adapter's single
bounded request for its weekly quota. `~/.codex/auth.json` and
`~/.claude/.credentials.json` are never touched; `~/.grok/auth.json` is read
(never written) only for the current bearer token — `refresh_token` is never
used and no credential value is ever logged or displayed.

## Usage

```
./agent-status-tui            # interactive, refreshes every 60s
./agent-status-tui --once     # render once and exit (scripting / embedding)
./agent-status-tui --interval 30
```

`Ctrl-C` quits interactive mode.

## What each column means

- **agent** — detected provider. Only agents with real local usage evidence
  are shown, newest activity first. Never ordered by token totals (they are not
  comparable across providers).
- **5h / week** — percent used, a compact bar, and a required `│` marker at the
  elapsed-time position inside the window, so usage-vs-time is visible at a
  glance. The bar colours by remaining capacity; the reset text colours by
  whether usage is ahead of elapsed time. A provider that does not expose a
  window shows an honest `--`, never a fabricated `0%`.
- **reset** — compact time until that window resets (`29m`, `3h12m`, `1d10h`,
  `now`). `ready` means the window is at exactly 0% with no reset yet;
  `unknown` means usage is known but no reset time is. A window at exactly 0%
  whose reset is still a full nominal duration away has not started — some
  providers publish a rolling placeholder reset for an untouched allowance —
  so it shows the nominal duration (`5h00m`) with no elapsed marker until the
  first use, after which the real countdown and marker take over.
- **data** — per-agent freshness. `LIVE` after a genuine live read (Codex
  rate limits, or the Grok weekly-quota request). Otherwise the age of that
  agent's cached source snapshot (`16s`, `3m`, `2h 5m`); if the Grok quota
  request fails, Grok falls back to an honest activity age. There is no
  global snapshot age.

The footer is one line — the local system date/time (minute precision) then
`· (<interval>) refresh in <countdown>s` — so a screenshot of the pane is
self-dating. In the full-width table its left edge is fixed at the 5h-meter
column, so a shorter countdown only trims the right-hand end. On narrow
terminals it is centred and degrades — date/time dropped first, then the
parenthesised interval; the line never wraps and the table geometry is never
disturbed.

## Providers

| provider | capacity source | live? | activity signal |
|---|---|---|---|
| **Codex** | `codex app-server --stdio` rate limits, else the newest local session rollout's last `token_count` | yes | session rollout mtimes + `~/.codex/history.jsonl` + `state_5.sqlite` `threads.updated_at` |
| **Claude** | Omarchy `agent-usage/claude-limits.json`, else `~/.claude.json` `cachedUsageUtilization` | no | newest `~/.claude/projects/**` transcript mtime + `~/.claude/history.jsonl` |
| **Grok** | live weekly quota from a single bounded `GET …/v1/billing?format=credits` authorised with the current `~/.grok/auth.json` bearer token; no 5h source | weekly only | `~/.grok/sessions/**/summary.json` `last_active_at`, else session-file mtimes, else `active_sessions.json` |

Grok exposes only a weekly window (`creditUsagePercent` + the current billing
period end), and only when the billing period is explicitly weekly. There is
still no local 5h source, so Grok's 5h column stays `--`. If the billing
request cannot be made (no/expired credential, HTTP, network, or schema
failure) Grok degrades to an activity-age row and its activity detection is
unaffected.

Within a confirmed-weekly response, `creditUsagePercent` and the period's
`end` are used independently: a live case exists where the period is
correctly weekly with a real `end` but `creditUsagePercent` itself is
missing/null. The official Grok TUI renders that as `0%` — a client-side
fallback, not a proven server value — which this project does not copy:
the percentage stays honestly unknown (`--`) while the genuinely reported
reset is still shown. Only when neither survives does Grok fall back to an
activity-age row.

## Keepalive

A standalone, self-contained component (`agentstatus/keepalive/`, no
dependency on the rest of this project) that pings Claude, Codex, and Grok
just often enough that a long-idle session doesn't lapse — and no more than
that.

**Fixed wall-clock cycles, not a floating timer.** Every cycle runs at
`:00` and `:30`, visiting agents in a fixed order (`claude`, `codex`,
`grok`), each agent's slot a minimum of 5 seconds after the previous one
(never more than one provider process at a time; readable, human-scannable
history). Sequential execution means a slot that legitimately takes longer
than 5 seconds — a real ping, or a slower provider-observation call —
pushes the next agent's slot later too; the offset is a floor, not a
millisecond guarantee. A cycle that runs long never drifts the next one —
every cycle's boundary is recomputed fresh from the wall clock, never added
to the previous one. (An earlier design
used a floating ~3001-second-since-last-activity interval per agent instead;
it is documented only as a historical constant now — see STATUS.md.)

**Activity-aware: a slot is not automatically a ping.** At its own slot,
each agent independently checks its own real activity (never another
agent's): if it was active in roughly the last 30 minutes, that slot
**skips** the ping entirely — no model turn, no cost. Otherwise it pings.
This is the whole point: minimise token spend while still guaranteeing a
ping within about 30 minutes of genuine idleness, per agent, independently.

```
./agent-status-tui keepalive              # interactive live table
./agent-status-tui keepalive --once       # one immediate, forced ping per agent (bypasses SKIP), no wait
./agent-status-tui keepalive --service    # quiet mode for systemd --user
./agent-status-tui keepalive --cycle-now  # one normal cycle right now (manual production diagnostic)
```

`--cycle-now` is not a fourth kind of ping: it is exactly one real cycle
(fixed order, the real 0/5/10s stagger, the real activity-aware PING/SKIP
decision, the real history/observation callback), just with this moment as
its boundary instead of waiting for the next `:00`/`:30` — then it exits.
It never touches or reschedules the normal `:00`/`:30` production cadence.

**A ping succeeding is not the same as the provider counting it as usage.**
Every real cycle slot (PING or SKIP alike, in any of the four modes above)
records one chronological history event per agent: whether it pinged or
skipped, the real activity timestamp that decision was based on, the ping's
technical outcome when it did ping (`OK`/`FAILED`, from its exit code
alone), plus whatever the provider's own native quota/reset surface reports
right afterward — a passive, bounded read (Codex's `account/rateLimits/read`
over `app-server --stdio`; Claude's local usage caches; Grok's weekly
billing endpoint) that never starts a second model turn. A technically
successful ping with no corroborating provider signal is shown as `OK` with
honest `--` quota columns — never labelled "delivered", and an unchanged
percentage is never read as proof of failure either (small pings can fall
below a provider's reporting resolution).

```
./agent-status-tui keepalive history               # every retained event, all agents, chronological
./agent-status-tui keepalive history --agent grok   # one agent only (drops the AGENT column)
./agent-status-tui keepalive monitor                # the same view, live-refreshing
```

`history`/`monitor` only ever read existing per-agent history files; neither
can cause a ping. History is capped at 16 days per agent and pruned
automatically as a side effect of normal keepalive operation (no separate
cron job): every recorded event atomically rewrites that agent's file with
anything older than 16 days already dropped. A corrupt/malformed history
file never disables keepalive — unreadable lines are skipped on read and
dropped for good the next time that agent successfully records an event.

See `COMMANDS.md` for the full command reference (systemd/journal
inspection, `doctor`, test commands) and `docs/calibrator-research.md` for
why an earlier, more elaborate probe-calibration subsystem was retired in
favour of this simpler, directly operational verification.

## Refresh model

Two clocks: a ~1 second display heartbeat (re-render only; the countdowns
tick) and a ~60 second real provider refresh. A heartbeat redraw never
re-polls a provider. Each provider's displayed source age is frozen between
real refreshes.

Provider polling is sequential and each poll is isolated: one provider failing
or timing out leaves its previous row in place (marked as an error after) and
never blanks the others.

## Narrow terminals

The normal-width layout above is authoritative. Every rendered row has exactly
one blank terminal column on each side. The table consumes the remaining width:
at 62 terminal columns that is 60 content columns, of which 27 are fixed fields
and spacing; the remaining 33 columns become 16- and 17-cell bars. Wider panes
grow both bars continuously instead of retaining fixed right padding.

Below 37 terminal columns (35 content columns), the display drops to a bar-less
`NAME 34%/46% AGE` text form. Very short panes retain a bounded subset ending
with the footer, and dimensions too small even for the two side margins produce
an empty frame. Rows are clipped rather than wrapped.

Interactive repainting writes no trailing newline. While the cursor is hidden,
it is explicitly parked on the final visible row one column inside the right
edge, avoiding deferred wrap and scroll at exactly 62×7.

## Environment overrides (testing / debugging)

`AGENT_STATUS_CODEX_HOME`, `AGENT_STATUS_CLAUDE_HOME`, `AGENT_STATUS_CLAUDE_JSON`,
`AGENT_STATUS_CLAUDE_PROJECTS`, `AGENT_STATUS_OMARCHY_CACHE`,
`AGENT_STATUS_GROK_HOME`.

`AGENT_STATUS_CLAUDE_JSON` and `AGENT_STATUS_OMARCHY_CACHE` are also honoured
by keepalive's own post-ping Claude observation
(`agentstatus/keepalive/observe.py`), which reads the same two local caches
independently (the keepalive package imports nothing from the dashboard).

## Tests

```
python3 -m unittest discover -s tests
```

Tests build throwaway temp home directories; no real files or credentials are
read.
