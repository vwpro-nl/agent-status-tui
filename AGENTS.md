# Repository rules

## Scope

`agent-status-tui` is one compact, read-only terminal dashboard that replaces
the separate per-agent status panes. It discovers the locally installed coding
agents (Codex, Claude, Grok), reads their local usage/rate-limit evidence, and
renders one row per agent in a shared table.

- Python standard library only. No third-party runtime or test dependencies.
- Read-only with respect to every provider's data. The only outbound network
  call is the Grok adapter's single bounded `GET` to the Grok billing endpoint
  for the weekly quota; there is no other network access.
- `~/.codex/auth.json` and `~/.claude/.credentials.json` are never opened. The
  Grok adapter reads `~/.grok/auth.json` for the current short-lived bearer
  token only: it never writes it, never reads or uses `refresh_token`, never
  refreshes credentials, and never logs, persists, or surfaces any value from
  it (error text is fixed strings).
- The Codex adapter may spawn a bounded, short-lived `codex app-server --stdio`
  subprocess purely to read rate limits. No adapter ever starts a model turn
  or a provider session.

## Structure

```
agent-status-tui          executable launcher (extension-less, matches siblings)
agentstatus/
  model.py                provider-neutral dataclasses + shared formatting
  render.py               the one shared renderer + cell-width helpers
  poll.py                 detection + refresh orchestration
  env.py                  env-overridable filesystem locations
  adapters/               one module per provider (codex, claude, grok)
tests/                    stdlib unittest; fixtures build throwaway temp homes
```

Adding a provider is one adapter module plus one entry in
`agentstatus/adapters/__init__.py`. The renderer and refresh loop never change.

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
