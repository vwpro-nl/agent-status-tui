# Repository rules

## Scope

`agent-status-tui` is one compact, read-only terminal dashboard that replaces
the separate per-agent status panes. It discovers the locally installed coding
agents (Codex, Claude, Grok), reads their local usage/rate-limit evidence, and
renders one row per agent in a shared table.

- Python standard library only. No third-party runtime or test dependencies.
- Read-only with respect to every provider's data. The only outbound network
  calls are single, bounded `GET`s to the Grok billing endpoint for the
  weekly quota — the dashboard's Grok adapter (per real refresh) and
  keepalive's own per-slot Grok observation (`agentstatus/keepalive/observe.py`,
  once per Grok cycle slot, whether that slot pinged or skipped) both make
  this same call; there is no other network access anywhere in the project.
- `~/.codex/auth.json` and `~/.claude/.credentials.json` are never opened. Any
  code that reads `~/.grok/auth.json` (the dashboard's Grok adapter, and
  keepalive's Grok observation) reads it for the current short-lived bearer
  token only: it never writes it, never reads or uses `refresh_token`, never
  refreshes credentials, and never logs, persists, or surfaces any value from
  it (error text is fixed strings).
- The dashboard's Codex adapter and keepalive's own per-slot Codex
  observation may each spawn a bounded, short-lived `codex app-server --stdio`
  subprocess purely to read rate limits. Nothing in this project ever starts
  a model turn or a provider session solely to measure status — the one
  exception is keepalive's own deliberate, minimal "Reply only: OK" ping
  turn, whose entire purpose is keeping the session alive, not measurement,
  and which keepalive skips entirely for a given agent/slot when that
  agent already has sufficiently recent real activity (see the keepalive
  structure notes below).

## Structure

```
agent-status-tui          executable launcher (extension-less, matches siblings)
agentstatus/
  model.py                provider-neutral dataclasses + shared formatting
  render.py               the one shared renderer + cell-width helpers
  poll.py                 detection + refresh orchestration
  env.py                  env-overridable filesystem locations
  grok_billing.py         shared Grok auth/billing helpers (dashboard adapter only)
  adapters/               one module per provider (codex, claude, grok) -- the dashboard
  doctor.py               read-only diagnostics for the keepalive component
  keepalive/              standalone pinger + chronological event history (see below)
tests/                    stdlib unittest; fixtures build throwaway temp homes
docs/                     permanent research/archival notes (not runtime code)
```

Adding a provider to the dashboard is one adapter module plus one entry in
`agentstatus/adapters/__init__.py`. The renderer and refresh loop never change.

### `agentstatus/keepalive/`

Self-contained: depends on no other subsystem in this project (not
`agentstatus.env`, not `agentstatus.adapters`, not `agentstatus.grok_billing`).
Where it needs the same technique another part of the project already
proved (e.g. reading Claude's local usage caches, or Grok's billing
endpoint), it reimplements that technique directly and minimally inside
this package rather than importing across the boundary.

```
core.py         clock-aligned scheduler: fixed :00/:30 cycles (CycleRunner),
                 fixed agent order + 5s stagger, activity-aware PING/SKIP
                 decision per agent (KeepaliveAgent) -- the one documented,
                 fixed-window interpretation this project performs; a
                 ping's own outcome is still read only as PingResult.ok/error
providers.py     one minimal "Reply only: OK" ping + one activity check per
                 provider
persistence.py   per-agent scheduler state (last *ping* outcome only -- a
                 SKIP never touches it)
history.py       per-agent chronological event history: atomic append +
                 16-day retention, tolerant/self-healing reads
observe.py       provider-native quota/reset snapshot taken once per slot
                 (PING or SKIP alike) -- passive, bounded, never a model turn
render.py        chronological table shared by `keepalive history` and
                 `keepalive monitor`
cli.py           `keepalive` / `keepalive history` / `keepalive monitor`
```

Production cadence is fixed wall-clock cycles at every `:00` and `:30`
(`CYCLE_SECONDS = 1800` in `core.py`), not a floating per-agent interval.
Every cycle visits agents in one fixed order (`claude`, `codex`, `grok`)
with a `AGENT_STAGGER_SECONDS = 5` offset between them. At its own slot, an
agent SKIPs (no ping, no model turn, no state write) when it has real
activity within `ACTIVITY_WINDOW_SECONDS = 1800` of that slot; otherwise it
PINGs. `LEGACY_INTERVAL_SECONDS = 3001` is kept only as a documented
historical constant (see STATUS.md) -- nothing schedules against it.

A slot's action (`PING`/`SKIP`), the ping's technical outcome (`OK`/`FAILED`)
when it did ping, and what a provider's own native quota/reset surface
reports afterward are always kept and displayed as separate facts -- a
successful ping is never presented as, or described as, "delivered".

## Reference projects

`codex-status-tui` and `claude-status-tui` are read-only references for proven
acquisition and freshness behavior. Do not runtime-import them; port only the
minimum logic needed. Do not modify them, `battery-status-tui`,
`omarchy-personal`, `~/.local/bin/monitor`, or any system configuration.

## Product vs. development attribution

The provider names CODEX, CLAUDE, and GROK are legitimate functional
identifiers and appear throughout the code and UI.

Do not add development attribution for AI assistants, tools, or vendors to
tracked project material: commit messages, authorship/co-author metadata,
generated-by notices, READMEs, or comments. Do not put internal task
identifiers into any tracked file.

## Commits

Do not commit or push unless a task explicitly authorizes it. When authorized,
keep messages factual and free of tooling attribution.

## Agent workflow

Work autonomously until the assigned task is technically complete.

- Do not ask questions about normal implementation choices, internal structure,
  tests, or preferences when they can reasonably be inferred from the task,
  repository, reference projects, existing tests, or safe project defaults.
- Make safe implementation decisions independently and report them afterward.
- Stop and ask only for a genuine blocker: required information that cannot be
  inferred, missing credentials or access, a destructive or irreversible
  action, or materially different external consequences that require a human
  decision.
- Do not ask "shall I continue?", present routine choice menus, or request
  intermediate permission for ordinary repository work.
- Run the relevant tests and validation checks before reporting completion.
- Do not commit or push unless the task explicitly authorizes it.

### Task identifiers

Every agent task must be given a TASK-ID containing a unique date and time
component. Preserve that TASK-ID in progress messages and the final report.

TASK-IDs are operational metadata only. Never write them into tracked project
files, source code, tests, documentation, commits, or other permanent project
material.

### ETA and progress

After initial inspection, provide an ETA as soon as the remaining scope can
reasonably be estimated. Do not invent an ETA before enough information is
available.

Show both the estimated local completion time and estimated remaining duration
prominently:

*** ETA: 12:35 (25m) ***

Continue working after reporting the ETA; it is progress information, not a
request for permission.

Continuously compare actual progress with the last reported estimate. If the
expected completion time or remaining duration changes by 10%
or more, report the revision immediately and prominently:

*** ETA REVISED: 12:45 (~~25m~~ 35m) ***

Use the local time of the execution environment for the completion time. The
duration in parentheses is the estimated remaining duration when reported. On
revision, strike through the previous duration as shown. Briefly state the
concrete reason for the revision and continue working.
Do not postpone a required ETA revision until the final report.
