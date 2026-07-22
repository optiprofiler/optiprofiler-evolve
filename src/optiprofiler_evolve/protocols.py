"""Internal extension contracts for the evolution controller.

The protocols deliberately expose narrow, capability-based contexts.  Owner
extensions are trusted Python code, but workers and candidate code never
receive these controller objects.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from threading import Event
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import SandboxConfig, ToolConfig, WorkerConfig, WorkersConfig
    from .models import CandidateRecord, EvaluationResult


EventEmitter = Callable[[str, str, Mapping[str, object] | None], None]


@dataclass(frozen=True)
class WorkerRequest:
    """A complete request for one coding-agent invocation."""

    worker: WorkerConfig
    workers: WorkersConfig
    sandbox: SandboxConfig
    workspace: Path
    tools_dir: Path
    broker: object
    prompt: str
    transcript: Path
    trace_dir: Path
    trace_context: Mapping[str, object] = field(default_factory=dict)
    cancellation_event: Event | None = None


@dataclass(frozen=True)
class WorkerOutcome:
    """Provider-independent lifecycle result from a coding worker."""

    returncode: int
    transcript: Path
    timed_out: bool = False
    native_trace: Path | None = None
    stderr_trace: Path | None = None
    trace_chunks: Path | None = None
    trace_outcome: Path | None = None
    capture_error: str | None = None
    cancelled: bool = False
    termination_reason: str | None = None


@dataclass(frozen=True)
class AgentJob:
    """One controller-authored research role executed in a sanitized workspace."""

    role: str
    job_id: str
    prompt: str
    inputs: Mapping[str, Path] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()
    worker_index: int = 0
    worker: WorkerConfig | None = None
    tools: ToolConfig | None = None
    timeout_seconds: int | None = None
    token_budget: int | None = None
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class AgentJobResult:
    """Lifecycle evidence from one controller-authored research role."""

    workspace: Path
    outcome: WorkerOutcome
    outputs: Mapping[str, Path]


@dataclass(frozen=True)
class VariantRequest:
    """A complete-tree or patch-based solver variant materialization request."""

    variant_id: str
    base: Path
    patches: tuple[Path, ...] = ()
    tree: Path | None = None
    expected_base_hash: str | None = None


@dataclass(frozen=True)
class VariantHandle:
    """Controller-owned identity for a gated immutable solver variant."""

    variant_id: str
    path: Path
    tree_hash: str
    base_tree_hash: str
    change_hashes: tuple[str, ...]


class PublicVariantEvaluator(Protocol):
    """Evaluate one gated variant without exposing the evaluator itself."""

    def __call__(
        self,
        handle: VariantHandle,
        label: str,
        *,
        reference: str | None = None,
    ) -> EvaluationResult: ...


@dataclass(frozen=True)
class ControllerServices:
    """The complete capability budget available to trusted workflow phases.

    Keep this surface capped at five operations. New methods require an
    architecture-constitution change and matching isolation tests.
    """

    run_trusted_agent: Callable[[AgentJob], AgentJobResult]
    materialize_variant: Callable[[VariantRequest], VariantHandle]
    evaluate_public_variant: PublicVariantEvaluator
    select_by_validation: Callable[[Sequence[str], int], tuple[str, ...]]
    register_finalist: Callable[[VariantHandle, Mapping[str, object]], str]


class WorkerAdapter(Protocol):
    """Launch a coding worker without exposing provider details to the engine."""

    name: str

    def run(self, request: WorkerRequest) -> WorkerOutcome: ...

    def provenance(self, workers: Sequence[WorkerConfig]) -> Mapping[str, object]: ...


class Evaluator(Protocol):
    """Trusted evaluation backend; the only component that resolves problem refs."""

    name: str
    deterministic: bool

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult: ...


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


class RetentionPolicy(Protocol):
    """Choose the bounded archive retained by one island."""

    name: str

    def retain(
        self,
        candidates: Sequence[CandidateRecord],
        capacity: int,
    ) -> tuple[CandidateRecord, ...]: ...


class ParentSampler(Protocol):
    """Choose one parent from an already ordered island archive."""

    name: str

    def select(
        self,
        population: Sequence[CandidateRecord],
        rng: Random,
    ) -> CandidateRecord: ...


@dataclass(frozen=True)
class PhaseContext:
    """Context for a trusted outer workflow phase."""

    run_dir: Path
    options: Mapping[str, object]
    artifacts: Mapping[str, object]
    invoke: Callable[[str], Mapping[str, object]]
    emit: EventEmitter
    services: ControllerServices


@dataclass(frozen=True)
class PhaseResult:
    """Named artifacts produced by one outer phase."""

    status: str = "succeeded"
    artifacts: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "skipped", "cancelled"}:
            raise ValueError(f"Unsupported phase status: {self.status!r}")


class Phase(Protocol):
    """One ordered outer workflow phase with explicit artifact dependencies."""

    name: str
    requires: frozenset[str]
    provides: frozenset[str]

    def run(self, context: PhaseContext) -> PhaseResult: ...


Factory = Callable[..., Any]


__all__: list[str] = []
