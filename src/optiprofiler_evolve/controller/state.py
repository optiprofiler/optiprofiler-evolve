"""Explicit run registries and staged workflow results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from optiprofiler_evolve.controller.contracts import VariantHandle
from optiprofiler_evolve.criteria.contracts import CandidateAssessment
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.models import (
    CandidateRecord,
    EvaluationResult,
    FinalistResult,
)
from optiprofiler_evolve.phases.contracts import PhaseResult
from optiprofiler_evolve.phases.hidden import HiddenResult
from optiprofiler_evolve.phases.prepare import PrepareResult
from optiprofiler_evolve.phases.validate import ValidateResult

_Result = TypeVar("_Result", bound=PhaseResult)


@dataclass(slots=True)
class RunState:
    """Authoritative run values; mutation belongs to RunCommits."""

    attempt_history: list[dict[str, Any]] = field(default_factory=list)
    candidates: dict[str, CandidateRecord] = field(default_factory=dict)
    populations: list[list[CandidateRecord]] = field(default_factory=list)
    phase_results: list[PhaseResult] = field(default_factory=list)
    hidden_started: bool = False
    direction_assignments: dict[int, dict[str, object]] = field(default_factory=dict)
    variant_handles: dict[str, VariantHandle] = field(default_factory=dict)
    variant_bases: dict[str, Path] = field(default_factory=dict)
    variant_public: dict[str, EvaluationResult] = field(default_factory=dict)
    variant_validation: dict[str, EvaluationResult] = field(default_factory=dict)
    research_finalists: dict[str, CandidateRecord] = field(default_factory=dict)
    validation_query_count: int = 0
    validation_selection_count: int = 0

    def _last_result(self, kind: type[_Result]) -> _Result | None:
        return next((result for result in reversed(self.phase_results) if isinstance(result, kind)), None)

    @property
    def artifacts(self) -> Mapping[str, object]:
        """Derived publication view; typed results remain the sole source of facts."""
        return MappingProxyType({key: value for result in self.phase_results
                                 for key, value in result.artifacts.items()})

    @property
    def data(self) -> DataPlan | None:
        result = self._last_result(PrepareResult)
        return result.data if result else None

    @property
    def assessor(self) -> CandidateAssessment | None:
        result = self._last_result(PrepareResult)
        return result.assessor if result else None

    @property
    def reference(self) -> Path | None:
        result = self._last_result(PrepareResult)
        return result.reference if result else None

    @property
    def champion(self) -> tuple[int, CandidateRecord] | None:
        result = self._last_result(ValidateResult)
        return result.champion if result else None

    @property
    def final_result(self) -> FinalistResult | None:
        result = self._last_result(HiddenResult)
        return result.final_result if result else None

    @property
    def final_solver(self) -> Path | None:
        result = self._last_result(HiddenResult)
        return result.final_solver if result else None
