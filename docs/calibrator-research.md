# Calibrator research (archived)

The calibrator subsystem has been removed from the active product. This
document is a compact, permanent record of what it was for, what it found,
and where to recover it. It is not a second copy of the implementation.

## Original goal

Starting at commit `c1a7a82` (18 September 2026), the calibrator set out to
answer a harder question than the dashboard needed: exactly how much
provider-native quota does one probe turn cost, and where does a provider's
5h/weekly usage boundary actually sit. It ran a sampling ladder of
increasing wait intervals around single-turn probes, captured
before/after provider-native quota where a provider exposed it, and used an
explicit boundary controller (explore -> confirm -> bracket -> done) to
narrow in on a cost/boundary estimate per provider.

## What was investigated

- **Provider-native measurement acquisition per provider:**
  - **Codex** — a passive, bounded `codex app-server --stdio` JSON-RPC
    `account/rateLimits/read` call, taken immediately before and after a
    probe turn. This is a read-only capacity query; it never starts a model
    turn itself. Both the 5h ("primary") and weekly ("secondary") windows
    come back from the same call.
  - **Claude** — no safe on-demand provider-quota refresh exists without
    reading Claude's own OAuth credentials, which this project will not do.
    The calibrator therefore never measured Claude provider-quota deltas; it
    used ccusage-derived token/cost telemetry (billing-block deltas) as a
    proxy signal instead, explicitly kept separate from any provider-quota
    claim.
  - **Grok** — a single bounded weekly billing `GET` (the same endpoint and
    bearer-token handling the dashboard's Grok adapter already uses),
    fetched before and after a probe turn. Grok's provider-native surface
    covers weekly usage only; there is no native 5h source for Grok.
- **Quota/reset-window handling** — 5h and weekly are independent meters. A
  window reset observed between "before" and "after" invalidates that
  meter's delta for the sample (it no longer reflects one probe's cost) but
  does not invalidate the other meter's delta. An unchanged percentage was
  deliberately never read as "the probe cost nothing" — it is a censored
  observation below the provider's reporting resolution, not proof of zero
  cost.
- **Isolation** — every adapter tried to attribute quota movement and local
  session-artifact changes strictly to its own probe (unique scratch
  working directories, rollout/session-file fingerprinting, thread-cwd
  attribution for Codex, session-file-path attribution for Grok) so a
  concurrent, unrelated agent session wouldn't be misread as probe cost.
- **Boundary/silence semantics** — the sampling ladder anchored its wait on
  `max(own last probe floor, own last detected real activity)`, with an
  explicit "unreliable" activity status (distinct from "no activity") so a
  broken activity source could never be silently read as "the user is
  idle". A resume after a restart enforced a fresh-silence floor so
  pre-restart quiet time never counted toward a new boundary sample.

## Why the calibrator is no longer the right operational instrument

The calibrator answered "how much does a probe cost, and where exactly does
a provider's boundary sit" — a research question. The operational question
that actually matters day to day is simpler and different: **did the
standalone keepalive's own scheduled pings technically succeed, and what do
the providers report afterward for their own native quota/reset state.**
That is what `agent-status-tui keepalive history` / `keepalive monitor` now
answer directly, without a sampling ladder, boundary controller, or
before/after cost-attribution machinery. The calibrator's own probes were
also a second, independent source of keepalive-relevant provider activity —
running both systems long-term would have meant two overlapping schedules
probing the same providers for related but differently-shaped answers.

## Relevant commits

`c1a7a82` (provider-neutral calibrator foundation) through `1a7e87d`
(controller-done/run-fix and classification normalization) — nine commits
building the calibrator core, its three provider adapters, its boundary
controller, and its multi-agent monitor. `e3a2a49` (standalone keepalive and
doctor) is the last commit before this archival and removal; it already
did not depend on the calibrator.

## Archive tag

`calibrator-research-archive` — an annotated tag on `e3a2a49`, the last
commit that contains the full calibrator implementation and its tests.

## Recovering the old implementation

```
git show calibrator-research-archive:agentstatus/calibrator/core.py
git checkout calibrator-research-archive -- agentstatus/calibrator tests/test_calibrator.py \
    tests/test_calibrator_claude.py tests/test_calibrator_codex.py \
    tests/test_calibrator_grok.py tests/test_calibrator_controller.py \
    tests/test_calibrator_monitor.py
git log calibrator-research-archive
```

Any of the above can be run against a worktree without touching the current
branch; the tag is a permanent, ordinary Git ref, not a special mechanism.

## What was reused going forward

The keepalive-history verification work introduced after this archival
reused the *proven acquisition techniques*, reimplemented directly and
minimally inside the self-contained `agentstatus/keepalive` package rather
than by importing the calibrator (which no longer exists) or coupling
keepalive to another subsystem:

- Codex's passive `account/rateLimits/read` JSON-RPC read over
  `codex app-server --stdio` (bounded timeout, no model turn started) —
  reused as a single post-ping snapshot instead of a before/after pair.
- Grok's single bounded weekly billing `GET` with the current short-lived
  bearer token, and the exact same credential rules (read `auth.json` only,
  never `refresh_token`, never log/persist/surface a credential value).
- The "distinguish unreliable/unknown from zero" discipline: a missing or
  unfetchable provider-native window is always represented as unavailable
  (`--`), never fabricated as `0%`.

What was **not** carried forward: the sampling ladder, the boundary
controller, before/after delta accounting, and probe-cost/isolation
attribution — none of that is needed to answer "did the ping technically
succeed, and what does the provider report now."
