"""Minimal, keepalive-only state.

A small, self-contained, versioned JSON schema. Atomic write: temp file +
fsync + os.replace, so a crash mid-write never corrupts the state file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATE_SCHEMA = "agent-status-keepalive-state/v1"


class PersistenceError(Exception):
    pass


def load_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"could not load keepalive state {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise PersistenceError(f"unsupported keepalive state: {path}")
    return value


def save_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        if os.write(fd, payload) != len(payload):
            raise OSError("short state write")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
