"""Public attempt capabilities and read-only iteration policy proposals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from optiprofiler_evolve.agents.contracts import WorkerOutcome
from optiprofiler_evolve.observability.events import EventEmitter

if TYPE_CHECKING:
    from optiprofiler_evolve.models import CandidateRecord, EvaluationResult


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
