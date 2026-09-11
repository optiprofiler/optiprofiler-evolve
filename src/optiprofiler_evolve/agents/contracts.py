"""Agent jobs, worker lifecycle results and backend contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol

from optiprofiler_evolve.observability.usage import UsageRecord

if TYPE_CHECKING:
    from optiprofiler_evolve.config import SandboxConfig, ToolConfig, WorkerConfig, WorkersConfig


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
    evaluation_tools: tuple[str, ...] = ()
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
    provider_gateway_outcome: str | None = None
    provider_gateway_request_count: int | None = None
    provider_gateway_manifest: Path | None = None
    solver_explanation: Path | None = None
    usage: UsageRecord = field(default_factory=UsageRecord.unavailable)


@dataclass(frozen=True)
class AgentJob:
    """One controller-authored research role executed in a sanitized workspace."""

    role: str
    job_id: str
    prompt: str
    inputs: Mapping[str, Path] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()
    recover_outputs_on_nonzero_exit: bool = False
    worker_index: int = 0
    worker: WorkerConfig | None = None
    tools: ToolConfig | None = None
    timeout_seconds: int | None = None
    token_budget: int | None = None
    max_budget_usd: float | None = None
    trace_links: Mapping[str, str | int | float | bool | None] = field(
        default_factory=dict
    )

    def render_prompt(self, generation_guidance: Callable[[], str] | None = None) -> str:
        """Ordinary jobs, including reviewers, retain their independent task."""
        return self.prompt


@dataclass(frozen=True)
class SolverJob(AgentJob):
    """A solver proposal or edit that must receive the configured criteria guidance."""

    def render_prompt(self, generation_guidance: Callable[[], str] | None = None) -> str:
        if generation_guidance is None:
            raise ValueError("Solver jobs require controller-bound criterion guidance.")
        return f"{self.prompt}\n\n{generation_guidance()}"


@dataclass(frozen=True)
class AgentJobResult:
    """Lifecycle evidence from one controller-authored research role."""

    workspace: Path
    outcome: WorkerOutcome
    outputs: Mapping[str, Path]
    missing_outputs: tuple[str, ...] = ()
    success: bool = True


class WorkerAdapter(Protocol):
    """Launch a coding worker without exposing provider details to the engine."""

    name: str

    def run(self, request: WorkerRequest) -> WorkerOutcome: ...

    def provenance(self, workers: Sequence[WorkerConfig]) -> Mapping[str, object]: ...
