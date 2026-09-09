"""Final report inputs and returned public result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.quality.complexity import ComplexityReport
from optiprofiler_evolve.shared.models import EvolveResult, FinalistResult
from optiprofiler_evolve.shared.protocols import PhaseContext, PhaseResult


@dataclass(frozen=True)
class ReportContext(PhaseContext):
    final_result: FinalistResult
    final_solver: Path
    check_complexity: Callable[..., ComplexityReport | None]


@dataclass(frozen=True, kw_only=True)
class ReportResult(PhaseResult):
    result: EvolveResult
