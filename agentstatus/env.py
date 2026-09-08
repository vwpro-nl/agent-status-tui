"""Resolved filesystem locations for every provider.

Every path is overridable through an environment variable so the test-suite
(and manual debugging) never touches a real home directory.  This mirrors the
``CODEX_HOME`` / ``CLAUDE_STATUS_*`` override pattern the sibling tools use.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Env:
    codex_home: Path
    claude_home: Path
    claude_json: Path
    claude_projects: Path
    omarchy_claude_cache: Path
    grok_home: Path

    @classmethod
    def resolve(cls) -> "Env":
        home = Path.home()

        def env_path(name: str, default: Path) -> Path:
            value = os.environ.get(name)
            return Path(value) if value else default

        claude_home = env_path("AGENT_STATUS_CLAUDE_HOME", home / ".claude")
        return cls(
            codex_home=env_path("AGENT_STATUS_CODEX_HOME", home / ".codex"),
            claude_home=claude_home,
            claude_json=env_path("AGENT_STATUS_CLAUDE_JSON", home / ".claude.json"),
            claude_projects=env_path(
                "AGENT_STATUS_CLAUDE_PROJECTS", claude_home / "projects"
            ),
            omarchy_claude_cache=env_path(
                "AGENT_STATUS_OMARCHY_CACHE",
                home / ".cache/omarchy/agent-usage/claude-limits.json",
            ),
            grok_home=env_path("AGENT_STATUS_GROK_HOME", home / ".grok"),
        )
