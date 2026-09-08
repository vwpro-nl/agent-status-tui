"""Small read-only helpers shared by the provider adapters."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Iterator


def load_json(path: Path) -> Any | None:
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def parse_iso(value: Any) -> float | None:
    """ISO-8601 -> epoch seconds.  ``None`` for anything unparseable
    (including the empty string, which some caches use for 'no reset')."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def epoch_from_ms_or_s(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    return value / 1000.0 if value > 10_000_000_000 else value


def newest_mtime(paths: Iterator[Path], limit: int = 4000) -> float | None:
    """Largest mtime among ``paths`` (already an iterator of files).  Bounded so
    a pathological directory cannot stall the refresh."""
    newest: float | None = None
    for count, path in enumerate(paths):
        if count >= limit:
            break
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def reverse_lines(path: Path, block_size: int = 64 * 1024) -> Iterator[str]:
    """Yield UTF-8 lines from a file in reverse without loading it all."""
    try:
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position:
            take = min(block_size, position)
            position -= take
            handle.seek(position)
            chunk = handle.read(take) + remainder
            parts = chunk.split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line.decode("utf-8", "replace")
        if remainder:
            yield remainder.decode("utf-8", "replace")


def last_json_value(path: Path, key: str, max_lines: int = 50) -> float | None:
    """Scan a .jsonl file from the end for the newest numeric ``key`` value,
    interpreted as an epoch (ms or s).  Used for per-prompt history logs."""
    best: float | None = None
    for count, raw in enumerate(reverse_lines(path)):
        if count >= max_lines:
            break
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        candidate = epoch_from_ms_or_s(record.get(key))
        if candidate is not None and (best is None or candidate > best):
            best = candidate
    return best


def combine(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None
