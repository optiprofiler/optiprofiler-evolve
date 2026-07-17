"""Solver source snapshots and entrypoint validation."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


_IGNORED_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".optiprofiler_evolve",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


@dataclass(frozen=True)
class InterfaceSpec:
    """A solver entrypoint relative to the solver root."""

    file: str
    function: str

    @classmethod
    def parse(cls, value: str) -> "InterfaceSpec":
        if value.count(":") != 1:
            raise ValueError("interface must use the form 'relative/file.py:function'.")
        file_name, function = value.split(":", 1)
        path = PurePosixPath(file_name)
        if path.is_absolute() or ".." in path.parts or not file_name:
            raise ValueError("The interface file must stay inside the solver directory.")
        if not function.isidentifier():
            raise ValueError("The interface function must be a valid identifier.")
        return cls(path.as_posix(), function)

    def detect_runtime(self, requested: str) -> str:
        if requested not in {"auto", "python", "matlab"}:
            raise ValueError("runtime must be auto, python, or matlab.")
        suffix = Path(self.file).suffix.lower()
        detected = {".py": "python", ".m": "matlab"}.get(suffix)
        if requested == "auto":
            if detected is None:
                raise ValueError(
                    f"Cannot infer runtime from {self.file!r}; set runtime explicitly."
                )
            return detected
        if detected is not None and requested != detected:
            raise ValueError(f"runtime={requested!r} conflicts with interface file {self.file!r}.")
        return requested


def copy_initial_source(initial: str | Path, destination: Path) -> Path:
    """Copy a file or directory into a new immutable seed snapshot."""

    source = Path(initial).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Initial solver does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}")
    if source.is_file():
        destination.mkdir(parents=True)
        shutil.copy2(source, destination / source.name)
    elif source.is_dir():
        _check_symlinks(source)
        shutil.copytree(source, destination, ignore=_copy_ignore)
    else:
        raise ValueError("initial must be a regular file or directory.")
    return destination


def copy_solver_tree(source: Path, destination: Path) -> None:
    """Materialize a private candidate or worker workspace."""

    source = source.resolve()
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}")
    _check_symlinks(source)
    shutil.copytree(source, destination, ignore=_copy_ignore)


def validate_interface(root: Path, interface: InterfaceSpec, runtime: str) -> None:
    """Check that the declared entrypoint exists without executing candidate code."""

    path = _inside(root, interface.file)
    if not path.is_file():
        raise ValueError(f"Interface file does not exist: {interface.file}")
    text = path.read_text(encoding="utf-8", errors="strict")
    if runtime == "python":
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            raise ValueError(f"Invalid Python interface file: {exc}") from exc
        definitions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if interface.function not in definitions:
            raise ValueError(
                f"Python interface {interface.function!r} is not defined at module level "
                f"in {interface.file!r}."
            )
    elif runtime == "matlab":
        pattern = re.compile(
            rf"^\s*function(?:\s+[^=\n]+\s*=)?\s*{re.escape(interface.function)}\s*\(",
            re.MULTILINE | re.IGNORECASE,
        )
        if pattern.search(text) is None:
            raise ValueError(
                f"MATLAB interface {interface.function!r} was not found in {interface.file!r}."
            )
    else:
        raise ValueError(f"Unsupported runtime: {runtime}")


def validate_candidate_imports(
    root: Path,
    *,
    runtime: str,
    forbidden: Iterable[str],
) -> None:
    """Enforce an auditable dependency ablation on Python candidate source.

    This is an experiment-policy check, not a security sandbox. The Docker boundary
    remains responsible for isolating untrusted worker execution.
    """

    blocked = tuple(sorted(set(forbidden)))
    if not blocked:
        return
    if runtime != "python":
        raise ValueError("Candidate import restrictions currently support Python only.")

    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if _ignored_relative(relative):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, line in _import_references(tree):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in blocked):
                violations.append(f"{relative.as_posix()}:{line}:{name}")
    if violations:
        raise ValueError(
            "Candidate uses imports forbidden by this experiment: " + ", ".join(violations)
        )


def _import_references(tree: ast.AST) -> set[tuple[str, int]]:
    references: set[tuple[str, int]] = set()
    imported_import_module = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            references.update((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            references.add((node.module, node.lineno))
            references.update((f"{node.module}.{alias.name}", node.lineno) for alias in node.names)
            if node.module == "importlib" and any(
                alias.name == "import_module" for alias in node.names
            ):
                imported_import_module = True
        elif isinstance(node, ast.Attribute):
            dotted = _dotted_attribute(node)
            if dotted:
                references.add((dotted, node.lineno))
        elif isinstance(node, ast.Call) and node.args:
            argument = node.args[0]
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                continue
            function = _dotted_attribute(node.func)
            if function in {"__import__", "importlib.import_module"} or (
                function == "import_module" and imported_import_module
            ):
                references.add((argument.value, node.lineno))
    return references


def _dotted_attribute(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def validate_tree_safety(root: Path, *, max_files: int, max_bytes: int) -> None:
    """Reject links, special files, and oversized worker output before host reads it."""

    root = root.resolve()
    files = 0
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in (*dirs, *names):
            path = Path(current, name)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"Candidate contains a symbolic link: {path.relative_to(root)}")
            if path.is_dir():
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"Candidate contains a special file: {path.relative_to(root)}")
            files += 1
            total += metadata.st_size
            if files > max_files:
                raise ValueError(f"Candidate exceeds the {max_files} file limit.")
            if total > max_bytes:
                raise ValueError(f"Candidate exceeds the {max_bytes} byte limit.")


def changed_files(before: Path, after: Path) -> tuple[str, ...]:
    """Return changed, created, and deleted regular-file paths."""

    before_hashes = _file_hashes(before)
    after_hashes = _file_hashes(after)
    return tuple(
        sorted(
            name
            for name in set(before_hashes).union(after_hashes)
            if before_hashes.get(name) != after_hashes.get(name)
        )
    )


def validate_edit_scope(paths: Iterable[str], editable: Iterable[str]) -> None:
    """Reject worker changes outside the declared solver surface."""

    patterns = tuple(_normalize_edit_pattern(pattern) for pattern in editable)
    if not patterns:
        raise ValueError("editable must contain at least one path or glob.")
    forbidden = [path for path in paths if not _matches_any(path, patterns)]
    if forbidden:
        raise ValueError(f"Worker changed files outside editable scope: {forbidden!r}")


def tree_hash(root: Path) -> str:
    """Create a stable digest of a solver snapshot."""

    digest = hashlib.sha256()
    for name, value in sorted(_file_hashes(root).items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _file_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _ignored_relative(path.relative_to(root)):
            continue
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    pure = PurePosixPath(path)
    for pattern in patterns:
        if pattern == ".":
            return True
        if path == pattern or path.startswith(pattern.rstrip("/") + "/"):
            return True
        if pure.match(pattern) or fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _normalize_edit_pattern(pattern: str) -> str:
    path = PurePosixPath(pattern)
    if path.is_absolute() or ".." in path.parts or not pattern:
        raise ValueError(f"Invalid editable path: {pattern!r}")
    return path.as_posix()


def _inside(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Path escapes solver root: {relative!r}")
    return candidate


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names if name in _IGNORED_NAMES or Path(name).suffix in _IGNORED_SUFFIXES
    }


def _ignored_relative(path: Path) -> bool:
    return any(part in _IGNORED_NAMES for part in path.parts) or path.suffix in _IGNORED_SUFFIXES


def _check_symlinks(root: Path) -> None:
    root = root.resolve()
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            path = Path(current, name)
            if path.is_symlink():
                raise ValueError(f"Solver source contains a symbolic link: {path}")


__all__: list[str] = []
