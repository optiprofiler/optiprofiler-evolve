"""Pack private owner evidence listed in owner/MANIFEST.json into tarballs.

This tool is never run automatically. It packs only the evidence roots named by
the derived manifest — one archive per ``--attempt``/``--role`` or a single
combined archive with ``--all`` — and refuses symlinks, absolute paths, ``..``
segments, and any path resolving outside the run directory. It never archives
the run directory itself and never records environment values or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from pathlib import Path


class PackError(RuntimeError):
    """A manifest entry or filesystem state that must not be packed."""


def load_manifest(run_dir: Path) -> dict:
    manifest_path = run_dir / "owner" / "MANIFEST.json"
    if not manifest_path.is_file():
        raise PackError(
            f"Missing {manifest_path}. Finish a run first; the manifest is written "
            "when the final owner view is rendered."
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "optiprofiler_evolve_owner_manifest/1":
        raise PackError("Unsupported owner manifest schema.")
    return payload


def _safe_root(run_dir: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise PackError(f"Absolute evidence path refused: {relative!r}")
    if ".." in candidate.parts:
        raise PackError(f"Parent-traversal evidence path refused: {relative!r}")
    if not candidate.parts:
        raise PackError("Empty evidence path refused.")
    if candidate.parts[0] == "public":
        raise PackError(f"Public bundle path refused in owner evidence: {relative!r}")
    resolved = (run_dir / candidate).resolve()
    run_resolved = run_dir.resolve()
    if resolved == run_resolved:
        raise PackError("Refusing to pack the whole run directory.")
    try:
        resolved.relative_to(run_resolved)
    except ValueError as exc:
        raise PackError(f"Evidence path escapes the run directory: {relative!r}") from exc
    return run_dir / candidate


def _iter_members(run_dir: Path, root: Path) -> list[Path]:
    if root.is_symlink():
        raise PackError(f"Symlink evidence root refused: {root}")
    if root.is_file():
        return [root]
    members = []
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(base) / name
            if path.is_symlink():
                raise PackError(f"Symlink inside evidence refused: {path}")
        for name in files:
            members.append(Path(base) / name)
    return members


def _entries_for(payload: dict, kind: str, wanted: set[str] | None) -> list[dict]:
    entries = []
    key = "attempt_id" if kind == "attempts" else "job_id"
    for entry in payload.get(kind, []):
        identifier = str(entry.get(key, ""))
        if wanted is None or identifier in wanted:
            entries.append(entry)
    return entries


def _pack(run_dir: Path, output: Path, entries: list[dict]) -> Path:
    members: list[tuple[Path, str]] = []
    for entry in entries:
        for item in entry.get("evidence", {}).values():
            root = _safe_root(run_dir, str(item.get("path", "")))
            if not root.exists():
                continue
            for member in _iter_members(run_dir, root):
                arcname = os.path.relpath(member, run_dir).replace(os.sep, "/")
                members.append((member, arcname))
    if not members:
        raise PackError("No evidence files matched the selection.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with tarfile.open(temporary, "w:gz") as archive:
        for member, arcname in sorted(members, key=lambda pair: pair[1]):
            archive.add(member, arcname=arcname, recursive=False)
    temporary.replace(output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Finished run directory")
    parser.add_argument(
        "--attempt",
        action="append",
        default=[],
        help="Pack one attempt's evidence (repeatable)",
    )
    parser.add_argument(
        "--role",
        action="append",
        default=[],
        help="Pack one trusted agent job's evidence (repeatable)",
    )
    parser.add_argument(
        "--all", action="store_true", help="Pack every attempt and agent job"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Destination directory (default: <run_dir>/owner/bundles)",
    )
    args = parser.parse_args(argv)
    run_dir = args.run_dir
    if not run_dir.is_dir():
        parser.error(f"run_dir does not exist: {run_dir}")
    if not args.all and not args.attempt and not args.role:
        parser.error("Select --attempt/--role explicitly, or pass --all.")
    output_dir = args.output_dir or run_dir / "owner" / "bundles"

    try:
        payload = load_manifest(run_dir)
        written = []
        if args.all:
            entries = _entries_for(payload, "attempts", None) + _entries_for(
                payload, "roles", None
            )
            written.append(_pack(run_dir, output_dir / "owner_evidence.tar.gz", entries))
        else:
            for attempt in args.attempt:
                entries = _entries_for(payload, "attempts", {attempt})
                if not entries:
                    raise PackError(f"Attempt not present in manifest: {attempt!r}")
                written.append(_pack(run_dir, output_dir / f"{attempt}.tar.gz", entries))
            for role in args.role:
                entries = _entries_for(payload, "roles", {role})
                if not entries:
                    raise PackError(f"Agent job not present in manifest: {role!r}")
                written.append(_pack(run_dir, output_dir / f"{role}.tar.gz", entries))
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
