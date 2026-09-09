"""Common inputs, staged results and the ordered phase contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "skipped", "cancelled"}:
            raise ValueError(f"Unsupported phase status: {self.status!r}")


class Phase(Protocol):
    """One ordered outer workflow phase with explicit artifact dependencies."""

    name: str
    requires: frozenset[str]
    provides: frozenset[str]

    def run(self, context: PhaseContext) -> PhaseResult: ...
