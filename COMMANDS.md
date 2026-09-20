# Commands — agent-status-tui

Practical command reference for using, monitoring, testing, and diagnosing
`agent-status-tui`.

Examples assume the project directory is:

`~/projects/agent-status-tui`

## Dashboard

Interactive unified agent status dashboard:

    ./agent-status-tui

Render once and exit:

    ./agent-status-tui --once

Use a different provider refresh interval, for example 30 seconds:

    ./agent-status-tui --interval 30

## Keepalive

Production cadence: fixed wall-clock cycles at every `:00` and `:30`, fixed
agent order (claude, codex, grok), each agent's slot a minimum of 5 seconds
after the previous one (a floor, not an exact guarantee — a slot that
genuinely takes longer, e.g. a real ping, pushes the next one later too).
At its own slot each agent independently checks its own real activity; if
it was active within the last ~30 minutes it **skips** the ping (no cost),
otherwise it pings. See `README.md` for the full rationale.

Interactive live table (in-place, one row per agent):

    ./agent-status-tui keepalive

One immediate, *forced* ping per agent, no wait, bypassing the
activity-aware SKIP decision entirely — a direct live/diagnostic check that
the ping mechanism itself still works, independent of whether a real
scheduled slot would have skipped it right now:

    ./agent-status-tui keepalive --once

Run exactly one *normal*, activity-aware production cycle right now (this
moment as the cycle's own boundary, not the next `:00`/`:30`) and exit — a
manual production diagnostic that goes through the real
`CycleRunner.run_cycle()`, the real fixed order and 0/5/10s stagger, the
real PING/SKIP decision per agent, and the real history/observation
callback, so every slot lands in the same history a real scheduled cycle
would produce. Never alters the normal `:00`/`:30` schedule:

    ./agent-status-tui keepalive --cycle-now

Quiet, non-interactive mode (what the systemd unit actually runs):

    ./agent-status-tui keepalive --service

Show the running user service:

    systemctl --user status agent-status-keepalive.service --no-pager -l

Show keepalive journal:

    journalctl --user -u agent-status-keepalive.service --no-pager

Show journal entries from a chosen time:

    journalctl --user -u agent-status-keepalive.service \
      --since 'YYYY-MM-DD HH:MM:SS' --no-pager -o short-iso

The keepalive service currently runs the project's `keepalive --service`
command. Consult the installed systemd unit and current project code before
changing its arguments or timing.

## Keepalive history and monitor

Print every retained event (up to 16 days, one per agent per cycle slot --
PING or SKIP alike), all agents interleaved chronologically. Columns:
`TIME AGENT ACTION LAST ACTIVITY PING 5H RESET WEEK RESET`:

    ./agent-status-tui keepalive history

One agent only (drops the AGENT column: `TIME ACTION LAST ACTIVITY PING 5H
RESET WEEK RESET`):

    ./agent-status-tui keepalive history --agent grok

The same view, live-refreshing (read-only: never pings, never constructs a
provider, never writes a history file):

    ./agent-status-tui keepalive monitor

    ./agent-status-tui keepalive monitor --once

    ./agent-status-tui keepalive monitor --refresh-interval 2 --limit 40

    ./agent-status-tui keepalive monitor --agent codex

`--state-dir` on any of the three keepalive subcommands points at an
alternate state root (mainly for testing) — it defaults to the same root the
running service itself uses
(`$XDG_STATE_HOME/agent-status-tui/keepalive`, normally
`~/.local/state/agent-status-tui/keepalive`), so a bare `history`/`monitor`
with no flags shows the real running service's own event log.

## Doctor

Read-only diagnostics for the keepalive component: systemd unit
install/enabled/active state, interval/provider configuration, per-agent
keepalive state, and per-agent keepalive history (directory writability,
parseable events, 16-day retention). Never repairs anything, never starts
a provider, never touches systemd beyond a read-only `is-enabled`/`is-active`:

    ./agent-status-tui doctor

## Tests

Run the complete stdlib unittest suite from the repository root:

    python3 -m unittest discover -s tests

## Calibrator (archived, not part of the active product)

The former probe-calibration subsystem (`calibrator status`/`history`/`run`/
`monitor`) was removed from the active product. For what it was, why it was
retired, the archive tag, and how to recover its exact implementation via
Git, see:

    docs/calibrator-research.md

## Repository inspection

Current working-tree state:

    git status --short

Current HEAD:

    git log -1 --date=local \
      --pretty=format:'%h  %ad  %s'

Recent history:

    git log --date=local \
      --pretty=format:'%h  %ad  %s' -25

Uncommitted changes:

    git diff

## Project documentation

For a new work session, read in this order:

1. `AGENTS.md` — permanent repository and agent rules.
2. `STATUS.md` — current project state, validation and next steps.
3. `COMMANDS.md` — practical commands and operational entry points.
4. `README.md` — product behaviour, architecture and usage details.

Code, tests, installed service definitions, and Git history remain the
technical authority when documentation is incomplete or outdated.

## Maintenance rule

Update this file when a useful user-facing, diagnostic, testing, maintenance,
or operational command is added or materially changed.

Keep this a practical command reference, not a development history.
