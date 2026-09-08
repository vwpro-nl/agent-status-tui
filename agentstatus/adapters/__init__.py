"""Provider adapters.

Each module exposes ``KEY``, ``DISPLAY_NAME``, ``detect(env) -> Detection`` and
``poll(env, now) -> AgentStatus``.  Registering a new provider is one import
plus one list entry here; the renderer and the refresh loop never change.
"""

from . import claude, codex, grok

ADAPTERS = (codex, claude, grok)

__all__ = ["ADAPTERS", "codex", "claude", "grok"]
