"""Explicit run registries and staged workflow results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optiprofiler_evolve.controller.contracts import VariantHandle
from optiprofiler_evolve.criteria.performance.contracts import Evaluator
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.models import (
    CandidateRecord,
    EvaluationResult,
    FinalistResult,
)


@dataclass(slots=True)
class RunState:
    """Authoritative run values; mutation belongs to RunCommits."""

    attempt_history: list[dict[str, Any]] = field(default_factory=list)
    candidates: dict[str, CandidateRecord] = field(default_factory=dict)
    populations: list[list[CandidateRecord]] = field(default_factory=list)
    artifacts: dict[str, object] = field(default_factory=dict)
    data: DataPlan | None = None
    evaluator: Evaluator | None = None
    reference: Path | None = None
    champion: tuple[int, CandidateRecord] | None = None
    final_result: FinalistResult | None = None
    final_solver: Path | None = None
    holdout_started: bool = False
    direction_assignments: dict[int, dict[str, object]] = field(default_factory=dict)
    variant_handles: dict[str, VariantHandle] = field(default_factory=dict)
    variant_bases: dict[str, Path] = field(default_factory=dict)
    variant_public: dict[str, EvaluationResult] = field(default_factory=dict)
    variant_validation: dict[str, EvaluationResult] = field(default_factory=dict)
    research_finalists: dict[str, CandidateRecord] = field(default_factory=dict)
    validation_query_count: int = 0
    validation_selection_count: int = 0
