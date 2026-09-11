"""Common inputs, staged results and the ordered phase contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from optiprofiler_evolve.config import EvolveConfig
from optiprofiler_evolve.controller.contracts import ControllerServices
from optiprofiler_evolve.observability.events import EventEmitter


@dataclass(frozen=True)
class PhaseContext:
    """Context for a trusted outer workflow phase."""

    run_dir: Path
    options: Mapping[str, object]
    artifacts: Mapping[str, object]
    emit: EventEmitter
    services: ControllerServices


@dataclass(frozen=True)
class PhaseResult:
    """Named artifacts produced by one outer phase."""

    status: str = "succeeded"
    artifacts: Mapping[str, object] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "skipped", "cancelled"}:
            raise ValueError(f"Unsupported phase status: {self.status!r}")
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))


class Phase(Protocol):
    """Ordered stage; requires also declares prerequisites supplied via typed contexts.

    Optional extensions are ``optional_requires: frozenset[str]`` (default empty)
    and ``validate_config(config: EvolveConfig) -> None`` (default no-op).
    Access these through the adapters below so minimal structural phases work.
    An optional producer may be absent, but if configured must precede its consumer.
    """

    name: str
    requires: frozenset[str]
    provides: frozenset[str]

    def run(self, context: PhaseContext) -> PhaseResult: ...


def phase_optional_requires(phase: Phase) -> frozenset[str]:
    return frozenset(getattr(phase, "optional_requires", ()))


def validate_phase_config(phase: Phase, config: EvolveConfig) -> None:
    validate = getattr(phase, "validate_config", None)
    if validate is not None:
        validate(config)


def validate_workflow(phases: tuple[Phase, ...], config: EvolveConfig) -> None:
    """Validate assembled dependencies in supplied order, without starting work."""
    produced = set().union(*(phase.provides for phase in phases))
    names: set[str] = set()
    available: set[str] = set()
    for phase in phases:
        if phase.name in names:
            raise ValueError(f"Duplicate phase name {phase.name!r}.")
        names.add(phase.name)
        duplicates = phase.provides.intersection(available)
        if duplicates:
            raise ValueError(
                f"Phase {phase.name!r} provides duplicate artifacts {sorted(duplicates)!r}."
            )
        missing = phase.requires.difference(available)
        if missing:
            raise ValueError(f"Phase {phase.name!r} requires missing artifacts {sorted(missing)!r}.")
        late = phase_optional_requires(phase).intersection(produced).difference(available)
        if late:
            raise ValueError(
                f"Phase {phase.name!r} has optional artifacts {sorted(late)!r} "
                "whose configured producers must run before it."
            )
        validate_phase_config(phase, config)
        available.update(phase.provides)
