"""Small immutable data models shared by the controller."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class EvaluationResult:
    """Canonical result of one candidate evaluation."""

    mode: str
    score: float
    candidate_score: float
    reference_score: float
    problem_count: int
    output_dir: Path
    success: bool = True
    error: str | None = None
    profile_scores: Any = None

    def as_dict(self, *, include_profile_scores: bool = False) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["output_dir"] = str(self.output_dir)
        if not include_profile_scores:
            value.pop("profile_scores", None)
        return value


@dataclasses.dataclass(frozen=True)
class CandidateRecord:
    """One immutable solver snapshot in the population database."""

    candidate_id: str
    island: int
    generation: int
    parent_id: str | None
    path: Path
    tree_hash: str
    public_score: float
    validation_score: float
    worker: str | None = None
    valid: bool = True
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["path"] = str(self.path)
        return value


@dataclasses.dataclass(frozen=True)
class FinalistResult:
    """Controller-only final evaluation for one fixed finalist."""

    candidate_id: str
    island: int
    public_score: float
    validation_score: float
    final_score: float
    output_dir: Path
    success: bool = True
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["output_dir"] = str(self.output_dir)
        return value


@dataclasses.dataclass(frozen=True)
class EvolveResult:
    """Return value of :func:`optiprofiler_evolve.evolve`."""

    run_dir: Path
    best_solver: Path
    best_candidate_id: str
    public_score: float
    validation_score: float
    final_score: float
    finalists: tuple[FinalistResult, ...]


__all__: list[str] = []
