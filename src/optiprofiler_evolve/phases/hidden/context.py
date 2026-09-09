"""One fixed champion and the controller-only holdout evaluator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.quality.complexity import ComplexityReport
from optiprofiler_evolve.shared.data import DataPlan
from optiprofiler_evolve.shared.models import CandidateRecord, FinalistResult
from optiprofiler_evolve.shared.protocols import Evaluator, PhaseContext, PhaseResult


@dataclass(frozen=True)
class HiddenContext(PhaseContext):
    champion: tuple[int, CandidateRecord]
    evaluator: Evaluator
    data: DataPlan
    check_complexity: Callable[..., ComplexityReport | None]


@dataclass(frozen=True, kw_only=True)
class HiddenResult(PhaseResult):
    final_result: FinalistResult
    final_solver: Path
