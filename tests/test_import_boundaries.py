"""Enforce the dependency direction declared in the architecture constitution.

``docs/architecture-constitution.md`` section 10 states these boundaries and
claims dependency tests guard them. This module is that guard. It reads imports
with :mod:`ast` rather than importing the package so that the check stays cheap
and cannot be defeated by import side effects.

Imports inside a ``TYPE_CHECKING`` block are ignored: they do not exist at
runtime, so they cannot create a cycle. That distinction matters here because
``phases/attempts/*`` deliberately annotates against controller runtime types
under ``TYPE_CHECKING`` while staying decoupled at runtime.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "optiprofiler_evolve"
PACKAGE = "optiprofiler_evolve"

# Constitution section 10, owner -> top-level packages it may never import.
FORBIDDEN: dict[str, frozenset[str]] = {
    # "Observability ... never import controller, phases, criteria or application."
    "observability": frozenset({"controller", "phases", "criteria", "application"}),
    # "Criteria ... never import controller registries, phases or application."
    "criteria": frozenset({"controller", "phases", "application"}),
    # "Phases ... never application or worker execution backends."
    "phases": frozenset({"application", "agents.sandbox", "agents.harness"}),
}

# Published contracts that the constitution explicitly lets dependants import.
# `controller.contracts` binds phases to controller capabilities, and
# `phases.contracts` publishes the phase protocol to the controller.
ALLOWED_SUBPACKAGES: dict[str, frozenset[str]] = {
    "phases": frozenset({"controller.contracts"}),
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _runtime_imports(path: Path) -> list[tuple[str, int]]:
    """Yield ``(dotted module, line)`` for imports that exist at runtime."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    typing_only: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and inner.module:
                    typing_only.add(inner.module)

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith(PACKAGE) or node.module in typing_only:
            continue
        found.append((node.module[len(PACKAGE) + 1 :], node.lineno))
    return found


def _violations(owner: str) -> list[str]:
    forbidden = FORBIDDEN[owner]
    allowed = ALLOWED_SUBPACKAGES.get(owner, frozenset())
    offending: list[str] = []
    for path in sorted((PACKAGE_ROOT / owner).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module, line in _runtime_imports(path):
            if module in allowed:
                continue
            top = module.split(".")[0]
            if top not in forbidden:
                continue
            # An allowed landing module may share a top-level name, e.g. phases
            # importing `controller.contracts` is permitted while
            # `controller.registry` is not.
            if any(module == candidate or module.startswith(candidate + ".") for candidate in allowed):
                continue
            offending.append(f"{_module_name(path)}:{line} -> {module}")
    return offending


@pytest.mark.parametrize("owner", sorted(FORBIDDEN))
def test_declared_dependency_direction(owner: str) -> None:
    offending = _violations(owner)
    assert not offending, (
        f"{owner!r} imports a package the architecture constitution forbids:\n  "
        + "\n  ".join(offending)
    )


def test_observability_never_imports_agent_implementations() -> None:
    """Observability may consume supplied contract values, never agent modules.

    ``observability -> agents`` was one half of a mutual dependency that made the
    layer graph a cycle; agent-produced value types now live at the package root.
    """

    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "observability").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module, line in _runtime_imports(path):
            if module.split(".")[0] == "agents":
                offenders.append(f"{_module_name(path)}:{line} -> {module}")
    assert not offenders, (
        "observability must not import the agent layer:\n  " + "\n  ".join(offenders)
    )
