"""Small explicit component registries.

This is intentionally a collection of dictionaries, not an auto-discovery or
dependency-resolution system.  Package entry points can be added when an
external plugin package actually exists.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from .config import ComponentConfig


_KINDS = ("phase", "step", "policy", "worker", "evaluator")
_REGISTRIES: dict[str, dict[str, Callable[..., Any]]] = {kind: {} for kind in _KINDS}


def register(kind: str, name: str, factory: Callable[..., Any], *, replace: bool = False) -> None:
    """Register one named factory in an explicit local registry."""

    if kind not in _REGISTRIES:
        raise ValueError(f"Unknown component kind: {kind!r}")
    if not name or not name.replace("-", "_").isidentifier():
        raise ValueError(f"Invalid component name: {name!r}")
    registry = _REGISTRIES[kind]
    if name in registry and not replace:
        raise ValueError(f"{kind} component {name!r} is already registered.")
    registry[name] = factory


def resolve(kind: str, name: str) -> Callable[..., Any]:
    """Resolve a registered factory with a useful error."""

    if kind not in _REGISTRIES:
        raise ValueError(f"Unknown component kind: {kind!r}")
    try:
        return _REGISTRIES[kind][name]
    except KeyError as exc:
        available = ", ".join(sorted(_REGISTRIES[kind])) or "<none>"
        raise KeyError(f"Unknown {kind} component {name!r}; available: {available}") from exc


def registered(kind: str) -> Mapping[str, Callable[..., Any]]:
    """Return a read-only snapshot for diagnostics and tests."""

    if kind not in _REGISTRIES:
        raise ValueError(f"Unknown component kind: {kind!r}")
    return dict(_REGISTRIES[kind])


def build(kind: str, spec: ComponentConfig) -> Any:
    """Instantiate one config reference or return its trusted component object."""

    target = spec._factory or resolve(kind, spec.name)
    is_instance = (
        spec._factory is not None
        and not inspect.isclass(target)
        and any(hasattr(target, attribute) for attribute in ("run", "propose", "evaluate"))
    )
    if is_instance:
        if spec.options:
            raise ValueError("Options cannot be applied to an already-created component object.")
        return target
    return target(**dict(spec.options))


__all__: list[str] = []
