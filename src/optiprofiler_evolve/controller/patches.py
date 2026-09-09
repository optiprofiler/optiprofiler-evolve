"""Bounded research patch validation and application."""

from __future__ import annotations

import subprocess
from pathlib import Path

from optiprofiler_evolve.files import safe_relative_path


def require_inside(path: Path, root: Path) -> None:
    root = root.resolve()
    path = path.resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"Research artifact escapes the run directory: {path}")


def validate_patch_paths(path: Path) -> None:
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t", 1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        safe_relative_path(raw)


def apply_patch(root: Path, patch: Path) -> None:
    attempts = ((), ("-p0",), ("-p1",))
    diagnostics: list[str] = []
    for strip in attempts:
        check = subprocess.run(
            ["git", "-C", str(root), "apply", "--check", *strip, str(patch)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if check.returncode != 0:
            diagnostics.append(check.stdout.strip())
            continue
        applied = subprocess.run(
            ["git", "-C", str(root), "apply", "--whitespace=nowarn", *strip, str(patch)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if applied.returncode == 0:
            return
        diagnostics.append(applied.stdout.strip())
    detail = next((item for item in diagnostics if item), "patch did not apply")
    raise ValueError(f"Patch conflict: {detail[:1000]}")
