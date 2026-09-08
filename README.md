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

## Tests

```
python3 -m unittest discover -s tests
```

Tests build throwaway temp home directories; no real files or credentials are
read.
