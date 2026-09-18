"""Versioned, durable calibrator state and chronological JSONL history."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

# v2 nests state into three conceptually distinct sections: "sampling" (the
# explorer ladder), "baseline" (adapter-owned provider assessment) and
# "controller" (reserved, opaque, for a future strategy layer). See
# agentstatus/calibrator/model.py for the full rationale.
STATE_SCHEMA = "agent-status-calibrator-state/v2"
CONTROLLER_STATE_SCHEMA = "agent-status-calibrator-controller-state/v1"
HISTORY_SCHEMA = "agent-status-calibrator-history/v1"


class PersistenceError(Exception):
    pass


def load_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"could not load calibrator state {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise PersistenceError(f"unsupported calibrator state: {path}")
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


def append_history(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema": HISTORY_SCHEMA, **value}
    payload = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("short history write")
        os.fsync(fd)
    finally:
        os.close(fd)


def load_history(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PersistenceError(f"could not load calibrator history {path}: {exc}") from exc
    records = []
    for number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PersistenceError(f"malformed calibrator history line {number}: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != HISTORY_SCHEMA:
            raise PersistenceError(f"unsupported calibrator history line {number}")
        records.append(value)
    return records


def load_histories(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Load and concatenate several per-agent history files.

    Each agent's history lives in its own file (no shared writer, no locking
    across agents). This is the seam a future chronological, multi-agent
    presentation merges through: callers sort the combined result themselves
    (see ``render.chronological``), this function only avoids re-reading and
    re-parsing each file per caller.
    """
    records: list[dict[str, Any]] = []
    for path in paths:
        records.extend(load_history(path))
    return records
