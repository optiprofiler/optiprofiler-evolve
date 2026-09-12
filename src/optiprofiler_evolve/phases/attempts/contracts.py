"""Public attempt capabilities and read-only iteration policy proposals."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from optiprofiler_evolve.agents.contracts import WorkerOutcome
from optiprofiler_evolve.observability.events import EventEmitter

if TYPE_CHECKING:
    from optiprofiler_evolve.models import CandidateRecord, EvaluationResult


def validate_attempt_steps(names: Iterable[str]) -> None:
    missing = {"mutate", "public_evaluate"}.difference(names)
    if missing:
        raise ValueError(f"workflow.attempt_steps must include {sorted(missing)!r}.")


_PUBLIC_MODES = frozenset({"smoke", "public", "public_score"})


@dataclass(frozen=True)
class PublicOnlyAssessment:
    """Bind one assessment callable to the worker-visible profiles.

    Lives here rather than in the controller because it is a phase-side adapter:
    it holds no controller state, imports no controller implementation, and only
    narrows the modes an attempt may request. Phases must not reach into
    controller implementations, which is what keeps the layer graph acyclic.
    """

    assess: Callable[..., EvaluationResult]
    candidate_id: str | None = None
    parent: Path | None = None

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        if mode not in _PUBLIC_MODES:
            raise ValueError("Worker assessment is public-only.")
        return self.assess(
            candidate,
            mode,
            output_dir,
            candidate_id=self.candidate_id,
            parent=self.parent,
        )


class AttemptCapabilities(Protocol):
    """Public-only capabilities available to an attempt step."""

    def run_worker(self) -> WorkerOutcome: ...

    def audit_candidate(self) -> tuple[str, ...]: ...

    def evaluate_public(self, mode: str) -> EvaluationResult: ...


@dataclass(frozen=True)
class AttemptContext:
    """Minimal context for one candidate attempt.

    It intentionally contains no population, validation split, hidden split,
    evaluator object, or complete experiment configuration.
    """

    attempt_id: str
    iteration: int
    island: int
    attempt_index: int
    parent_id: str
    parent_score: float
    workspace: Path
    worker_name: str
    rng_seed: int
    prior_results: tuple[StepResult, ...]
    capabilities: AttemptCapabilities
    emit: EventEmitter


@dataclass(frozen=True)
class StepResult:
    """One attempt step's immutable proposal and evidence."""

    verdict: str = "pass"
    metrics: Mapping[str, object] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in {"pass", "reject", "repaired"}:
            raise ValueError(f"Unsupported step verdict: {self.verdict!r}")


class AttemptStep(Protocol):
    """Transform or inspect one candidate without changing population state."""

    name: str

    def run(self, context: AttemptContext) -> StepResult: ...


@dataclass(frozen=True)
class PopulationEdit:
    """A policy proposal that only the engine may apply.

    ``budget`` is reserved for a future scheduler. The alpha engine rejects a
    nonempty value instead of silently recording an ineffective experiment.
    """

    kill: tuple[str, ...] = ()
    migrate: tuple[tuple[str, int], ...] = ()
    budget: Mapping[str, object] = field(default_factory=dict)
    stop: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class IterationView:
    """Read-only population snapshot presented after an iteration barrier."""

    iteration: int
    populations: tuple[tuple[CandidateRecord, ...], ...]
    attempt_ids: tuple[str, ...]
    rng_seed: int


class AfterIterationPolicy(Protocol):
    """Propose population edits after all attempts in one iteration finish."""

    name: str

    def propose(self, view: IterationView) -> PopulationEdit: ...
