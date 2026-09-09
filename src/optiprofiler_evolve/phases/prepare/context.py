"""Inputs and staged result of preparation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.quality.complexity import ComplexityChecker
from optiprofiler_evolve.shared.config import EvolveConfig
from optiprofiler_evolve.shared.data import DataPlan
from optiprofiler_evolve.shared.models import CandidateRecord
from optiprofiler_evolve.shared.protocols import Evaluator, PhaseContext, PhaseResult
from optiprofiler_evolve.shared.solver import InterfaceSpec


@dataclass(frozen=True)
class PrepareContext(PhaseContext):
    initial: Path
    interface: InterfaceSpec
    runtime: str
    editable: tuple[str, ...]
    config: EvolveConfig
    evaluator_factory: Callable[..., Evaluator]
    validate_candidate: Callable[[Path], None]


@dataclass(frozen=True, kw_only=True)
class PrepareResult(PhaseResult):
    data: DataPlan
    reference: Path
    evaluator: Evaluator
    complexity: ComplexityChecker | None
    seed: CandidateRecord
