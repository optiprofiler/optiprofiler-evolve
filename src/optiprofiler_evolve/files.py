"""JSON persistence, file hashes and relative-path validation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any


def read_regular_bytes(path: Path, max_bytes: int) -> bytes:
    """Read a bounded regular file without following its final symlink.

    Callers own the parent-directory boundary and any format-specific decoding.
    """
    if max_bytes < 0:
        raise ValueError("File size limit must be nonnegative.")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Expected a regular file, not a symlink or special file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Opened file is not a regular file.")
        if opened.st_size > max_bytes:
            raise ValueError(f"File exceeds the {max_bytes}-byte size limit.")
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"File exceeds the {max_bytes}-byte size limit.")
    return raw


def read_json_object(path: Path) -> dict[str, Any]:
    """Read one JSON object with a path-specific error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON object from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def safe_relative_path(value: str) -> PurePosixPath:
    """Validate one role-workspace relative path."""

    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value or value == ".":
        raise ValueError(f"Unsafe relative path: {value!r}")
    return path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def append_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, default=str) + "\n")
