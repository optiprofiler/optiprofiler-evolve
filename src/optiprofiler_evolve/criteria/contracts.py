"""Quality evidence, independent of candidate admission and backend execution."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from optiprofiler_evolve.config import freeze_value

if TYPE_CHECKING:
    from optiprofiler_evolve.config import WorkerConfig
    from optiprofiler_evolve.criteria.complexity.analyzer import ComplexityChecker
    from optiprofiler_evolve.criteria.integrity.reviewer import IntegrityReviewDecision
    from optiprofiler_evolve.models import EvaluationResult

Profile = Literal["smoke", "public", "validation", "hidden"]


class CandidateAssessment(Protocol):
    """Controller assessment, distinct from the numerical Evaluator backend."""

    def evaluate(
        self, candidate: Path, mode: str, output_dir: Path, *,
        candidate_id: str | None = None, parent: Path | None = None,
        mutation_transcript: Path | None = None, mutation_worker: WorkerConfig | None = None,
        reference: Path | None = None,
    ) -> EvaluationResult: ...


@dataclass(frozen=True)
class GuidanceRequest:
    """Public scoring rules only; never source paths, evaluation data or results."""

    profiles: tuple[Literal["smoke", "public", "validation"], ...]
    options: Mapping[str, object]
    weight: float
    mode: Literal["warn", "enforce"]
    feedback_mode: str

    def __post_init__(self) -> None:
        if not self.profiles or any(p not in {"smoke", "public", "validation"} for p in self.profiles):
            raise ValueError("Generation guidance cannot use hidden assessment settings.")
        object.__setattr__(self, "options", freeze_value(self.options))


@dataclass(frozen=True)
class AssessmentRequest:
    candidate: Path
    tree_hash: str
    profile: Profile
    output_dir: Path
    options: Mapping[str, object] = field(default_factory=dict)
    input_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", freeze_value(self.options))


@dataclass(frozen=True)
class CriterionBackends:
    """Trusted measurement ports; no population, commits, or workflow authority."""

    benchmark: Callable[[AssessmentRequest], EvaluationResult]
    review: Callable[[AssessmentRequest], tuple[IntegrityReviewDecision, Path]]
    complexity: ComplexityChecker | None


@dataclass(frozen=True)
class CriterionResult:
    status: Literal["succeeded", "failed", "skipped"]
    score: float | None = None
    satisfied: bool | None = None
    metrics: Mapping[str, object] = field(default_factory=dict)
    evidence: tuple[Path, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "skipped"}:
            raise ValueError(f"Unknown criterion status: {self.status!r}")
        if self.score is not None and (
            isinstance(self.score, bool) or not math.isfinite(self.score)
        ):
            raise ValueError("Criterion scores must be finite real numbers.")
        if self.status != "succeeded" and (self.score is not None or self.satisfied is not None):
            raise ValueError("Unexecuted or failed criteria cannot claim a score or verdict.")
        if self.status == "succeeded" and self.satisfied is None:
            raise ValueError("Completed criteria must report their constraint verdict.")
        object.__setattr__(self, "metrics", freeze_value(self.metrics))


class Criterion(ABC):
    """A higher-is-better score and an independent constraint verdict."""

    version = "1"
    guidance_fields: tuple[str, ...] = ()

    @classmethod
    def bind(cls, backends: CriterionBackends, *, mode: str) -> Criterion:
        """Construct a criterion; backend users override this explicit binding hook."""
        return cls()

    @classmethod
    @abstractmethod
    def generation_guidance(cls, request: GuidanceRequest) -> str:
        """Describe this standard using only explicitly public option fields."""

    @abstractmethod
    def assess(self, request: AssessmentRequest) -> CriterionResult:
        """Assess one controller-bound snapshot without admitting it."""
