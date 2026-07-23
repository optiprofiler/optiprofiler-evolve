"""Evidence access for the owner console: paths, previews, diffs, manifest.

This module contains no HTML. It reads run-directory evidence within strict
bounds and never resolves outside the run directory. Rendering and page
orchestration live in :mod:`.owner_views`.
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .viewers import _atomic_write, _mapping, _sequence

PREVIEW_MAX_BYTES = 65_536
PREVIEW_HEAD_LINES = 100
PREVIEW_TAIL_LINES = 60
TRANSCRIPT_MAX_ENTRIES = 80
TRANSCRIPT_ENTRY_CHARS = 400
DIFF_MAX_FILES = 200
DIFF_FILE_MAX_BYTES = 200_000
DIFF_PATCH_MAX_BYTES = 512_000
DIFF_PREVIEW_LINES = 160


def parse_transcript(path: Path) -> tuple[list[tuple[str, str]], str, bool]:
    """Parse a JSONL transcript into bounded (label, text) entries.

    Returns ``(entries, fallback_text, truncated)``; ``entries`` is empty when
    no line parses as JSON, in which case ``fallback_text`` holds a bounded
    plain-text preview instead.
    """

    raw = path.read_bytes()
    truncated = len(raw) > PREVIEW_MAX_BYTES
    text = raw[:PREVIEW_MAX_BYTES].decode("utf-8", errors="replace")
    entries: list[tuple[str, str]] = []
    parsed_any = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        parsed_any = True
        label_bits = [
            str(payload.get(key))
            for key in ("type", "role", "subtype", "tool", "name")
            if payload.get(key)
        ]
        label = " / ".join(label_bits[:3]) or "entry"
        snippet = _extract_text(payload)
        entries.append((label, snippet[:TRANSCRIPT_ENTRY_CHARS]))
        if len(entries) >= TRANSCRIPT_MAX_ENTRIES:
            truncated = True
            break
    if parsed_any:
        return entries, "", truncated
    preview, text_truncated = bounded_lines(text)
    return [], preview, truncated or text_truncated


def _extract_text(payload: Mapping[str, Any]) -> str:
    for key in ("text", "content", "message", "prompt", "output", "input", "data"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, Mapping):
            nested = _extract_text(value)
            if nested:
                return nested
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping):
                    nested = _extract_text(item)
                    if nested:
                        parts.append(nested)
            if parts:
                return "\n".join(parts)
    return json.dumps(payload, sort_keys=True)[:TRANSCRIPT_ENTRY_CHARS]


def preview_stream(path: Path) -> tuple[str, bool] | None:
    """Bounded text preview of a raw capture stream, or None if unavailable."""

    if not path.is_file() or path.is_symlink():
        return None
    raw = path.read_bytes()
    truncated = len(raw) > PREVIEW_MAX_BYTES
    text = raw[:PREVIEW_MAX_BYTES].decode("utf-8", errors="replace")
    preview, line_truncated = bounded_lines(text)
    return preview, truncated or line_truncated


def bounded_lines(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= PREVIEW_HEAD_LINES + PREVIEW_TAIL_LINES:
        return text, False
    head = lines[:PREVIEW_HEAD_LINES]
    tail = lines[-PREVIEW_TAIL_LINES:]
    omitted = len(lines) - PREVIEW_HEAD_LINES - PREVIEW_TAIL_LINES
    return "\n".join([*head, f"... [{omitted} lines omitted] ...", *tail]), True


def unified_diff(parent: Path, candidate: Path) -> tuple[str, bool]:
    """Bounded text diff between two candidate trees; symlinks are skipped."""

    names: set[str] = set()
    for root in (parent, candidate):
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in {"__pycache__", ".git"}]
            for name in files:
                path = Path(base) / name
                if path.is_symlink():
                    continue
                names.add(str(path.relative_to(root)))
    chunks: list[str] = []
    total = 0
    truncated = False
    for relative in sorted(names)[:DIFF_MAX_FILES]:
        before = _read_text_bounded(parent / relative)
        after = _read_text_bounded(candidate / relative)
        if before is None and after is None:
            continue
        if before == after:
            continue
        diff = "".join(
            difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                (after or "").splitlines(keepends=True),
                fromfile=f"parent/{relative}",
                tofile=f"candidate/{relative}",
            )
        )
        if not diff:
            continue
        total += len(diff)
        chunks.append(diff)
        if total > DIFF_PATCH_MAX_BYTES:
            truncated = True
            break
    if len(names) > DIFF_MAX_FILES:
        truncated = True
    return "".join(chunks), truncated


def _read_text_bounded(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > DIFF_FILE_MAX_BYTES:
        return f"[file larger than {DIFF_FILE_MAX_BYTES} bytes; diff skipped]\n"
    data = path.read_bytes()
    if b"\x00" in data:
        return "[binary file; diff skipped]\n"
    return data.decode("utf-8", errors="replace")


def resolve_recorded_path(recorded: str, run_dir: Path) -> Path | None:
    """Map a path recorded at run time onto the current run directory."""

    if not recorded:
        return None
    parts = PurePosixPath(recorded.replace("\\", "/")).parts
    for start in range(len(parts)):
        suffix = parts[start:]
        if not suffix or ".." in suffix:
            continue
        candidate = run_dir.joinpath(*suffix)
        if candidate.exists() and not candidate.is_symlink():
            try:
                candidate.resolve().relative_to(run_dir.resolve())
            except ValueError:
                return None
            return candidate
    return None


def resolve_role_output(workspace: Path, name: str) -> Path | None:
    """Resolve a declared role output inside its workspace, or None.

    Declared names come from the event ledger; treat them as untrusted and
    refuse absolute paths, parent traversal, symlinks, and escapes.
    """

    pure = PurePosixPath(str(name).replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    target = workspace.joinpath(*pure.parts)
    if not target.exists() or target.is_symlink():
        return None
    try:
        target.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return None
    return target


def write_owner_manifest(state: Mapping[str, Any], run_dir: Path) -> None:
    """Write the derived evidence index. It never holds content or secrets."""

    attempts = []
    for value in _sequence(state.get("attempts")):
        attempt = _mapping(value)
        attempt_id = str(attempt.get("attempt_id"))
        evidence = {}
        for name, relative in (
            ("workspace", f"workspaces/{attempt_id}"),
            ("candidate_snapshot", f"candidates/{attempt_id}"),
            ("transcript", f"transcripts/{attempt_id}.jsonl"),
            ("traces", f"traces/{attempt_id}"),
            ("evaluations", f"controller/evaluations/{attempt_id}"),
            ("final_evaluation", f"controller/final_evaluations/{attempt_id}"),
            ("integrity_reviews", f"controller/integrity_reviews/{attempt_id}"),
            ("broker", f"controller/brokers/{attempt_id}"),
        ):
            target = run_dir / relative
            if target.exists() and not target.is_symlink():
                evidence[name] = {"path": relative, "bytes": tree_bytes(target)}
        attempts.append({"attempt_id": attempt_id, "evidence": evidence})
    roles = []
    for value in _sequence(state.get("roles")):
        role = _mapping(value)
        job_id = str(role.get("job_id"))
        role_name = str(role.get("role", ""))
        evidence = {}
        for name, relative in (
            ("transcript", f"research/transcripts/{role_name}/{job_id}.jsonl"),
            ("traces", f"research/traces/{role_name}/{job_id}"),
            ("workspace", f"research/roles/{role_name}/{job_id}"),
        ):
            target = run_dir / relative
            if target.exists() and not target.is_symlink():
                evidence[name] = {"path": relative, "bytes": tree_bytes(target)}
        roles.append({"job_id": job_id, "role": role_name, "evidence": evidence})
    manifest = {
        "schema": "optiprofiler_evolve_owner_manifest/1",
        "note": (
            "Derived evidence index for pack_owner_evidence.py. The event ledger "
            "and the files on disk remain the sources of truth."
        ),
        "attempts": attempts,
        "roles": roles,
    }
    _atomic_write(
        run_dir / "owner" / "MANIFEST.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def tree_bytes(root: Path) -> int:
    if root.is_file():
        return root.stat().st_size
    total = 0
    for base, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(base) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def compact_text(value: object) -> str:
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "…"


__all__: list[str] = []
