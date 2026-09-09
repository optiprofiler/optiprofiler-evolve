"""Controller-only inputs and champion proposal."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from optiprofiler_evolve.quality.complexity import ComplexityReport
from optiprofiler_evolve.shared.models import CandidateRecord
from optiprofiler_evolve.shared.protocols import PhaseContext, PhaseResult


@dataclass(frozen=True)
class ValidateContext(PhaseContext):
    populations: tuple[tuple[CandidateRecord, ...], ...]
    research_finalists: tuple[CandidateRecord, ...]
    fixed_finalists: Callable[[list[list[CandidateRecord]]], list[tuple[int, CandidateRecord]]]
    assess_complexity: Callable[..., ComplexityReport | None]


@dataclass(frozen=True, kw_only=True)
class ValidateResult(PhaseResult):
    champion: tuple[int, CandidateRecord]
