"""Detection + refresh orchestration.

Deliberately simple: adapters are polled sequentially.  The Codex adapter
already bounds its own subprocess, and the Claude/Grok adapters are fast local
reads, so there is no worker pool, no backoff state machine.  The only
robustness requirement -- one failing provider must not blank the table -- is
met by catching per-adapter exceptions and reusing that provider's previous
row.
"""

from __future__ import annotations

import dataclasses

from .adapters import ADAPTERS
from .env import Env
from .model import AgentStatus

HIDE_AFTER_SECONDS = 30 * 24 * 60 * 60


def _error_status(module, detection, message: str) -> AgentStatus:
    return AgentStatus(
        key=module.KEY,
        display_name=module.DISPLAY_NAME,
        five_hour=None,
        weekly=None,
        freshness_kind="none",
        source_age=None,
        last_activity=detection.last_activity if detection else None,
        availability="error",
        detail=message[:60],
    )


def collect(
    env: Env,
    now: float,
    previous: dict[str, AgentStatus] | None = None,
) -> list[AgentStatus]:
    previous = previous or {}
    statuses: list[AgentStatus] = []

    for module in ADAPTERS:
        try:
            detection = module.detect(env)
        except Exception:  # a broken adapter must never abort the whole scan
            continue
        if not detection.ever_used:
            continue

        try:
            status = module.poll(env, now)
        except Exception as exc:
            prior = previous.get(module.KEY)
            if prior is not None:
                status = dataclasses.replace(
                    prior, availability="error", detail=str(exc)[:60]
                )
            else:
                status = _error_status(module, detection, str(exc))
        statuses.append(status)

    visible = [s for s in statuses if not _should_hide(s, now)]
    visible.sort(
        key=lambda s: (s.last_activity is not None, s.last_activity or 0.0),
        reverse=True,
    )
    return visible


def _should_hide(status: AgentStatus, now: float) -> bool:
    """v1 rule: a provider last used more than 30 days ago that currently has
    no capacity window is not worth a row."""
    if status.five_hour is not None or status.weekly is not None:
        return False
    if status.last_activity is None:
        return False
    return status.last_activity < now - HIDE_AFTER_SECONDS
