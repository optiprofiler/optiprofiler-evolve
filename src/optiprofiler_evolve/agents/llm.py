"""Unified, authority-bound Agent invocation and trace lifecycle."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from optiprofiler_evolve.agents.contracts import (
    AgentJob,
    AgentJobResult,
    WorkerAdapter,
    WorkerOutcome,
    WorkerRequest,
)
from optiprofiler_evolve.agents.docker_runtime import read_gateway_outcome
from optiprofiler_evolve.agents.jobs import trace_event_scope as _trace_event_scope
from optiprofiler_evolve.files import safe_relative_path
from optiprofiler_evolve.observability.trace_ledger import TraceLedger
from optiprofiler_evolve.observability.traces import finalize_adapter_trace


class LLMClient:
    """Execute jobs with controller-bound authority and retain invocation evidence."""

    def __init__(
        self,
        *,
        adapter: WorkerAdapter,
        request: WorkerRequest,
        ledger: TraceLedger,
        emit: Callable[..., None],
        record_usage: Callable[[WorkerRequest, WorkerOutcome], None],
        generation_guidance: Callable[[], str] | None = None,
    ) -> None:
        self.adapter = adapter
        self.request = request
        self.ledger = ledger
        self.cancellation_event = request.cancellation_event or threading.Event()
        self.emit = emit
        self.record_usage = record_usage
        self.generation_guidance = generation_guidance

    def run(
        self, job: AgentJob, *, on_started: Callable[[WorkerRequest], None] | None = None,
    ) -> AgentJobResult:
        request = replace(self.request, prompt=job.render_prompt(self.generation_guidance))
        if on_started is not None:
            on_started(request)
        outcome = self._execute(request)
        outputs = {}
        for relative in job.expected_outputs:
            path = (request.workspace / safe_relative_path(relative)).resolve()
            if request.workspace.resolve() in path.parents and path.exists():
                outputs[relative] = path
        missing = tuple(sorted(set(job.expected_outputs).difference(outputs)))
        success = (
            not outcome.timed_out
            and not outcome.cancelled
            and not missing
            and (outcome.returncode == 0 or job.recover_outputs_on_nonzero_exit)
        )
        return AgentJobResult(request.workspace, outcome, outputs, missing, success)

    def _execute(self, request: WorkerRequest) -> WorkerOutcome:
        """Invoke one adapter behind a controller-owned trace boundary."""

        paths = self.ledger.prepare(
            root=request.trace_dir,
            prompt=request.prompt,
            command=["<worker-adapter>", self.adapter.name],
            worker=request.worker,
            workers=request.workers,
            sandbox=request.sandbox,
            workspace=request.workspace,
            context=request.trace_context,
            secret_values=request.worker.env,
        )
        gateway_expected = request.worker.provider_gateway is not None
        gateway_scope = _trace_event_scope(request.trace_context)
        if gateway_expected:
            self.emit("provider_gateway_started", "running", scope=gateway_scope)
        try:
            outcome = self.adapter.run(request)
        except Exception as exc:
            request.transcript.parent.mkdir(parents=True, exist_ok=True)
            request.transcript.write_text(
                f"[controller] worker adapter failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            outcome = WorkerOutcome(
                1,
                request.transcript,
                cancelled=self.cancellation_event.is_set(),
                termination_reason="adapter_exception",
                capture_error=f"adapter: {type(exc).__name__}: {exc}",
            )
        cancelled = outcome.cancelled or self.cancellation_event.is_set()
        paths = finalize_adapter_trace(
            paths,
            transcript=outcome.transcript,
            native_trace=outcome.native_trace,
            stderr_trace=outcome.stderr_trace,
            returncode=outcome.returncode,
            timed_out=outcome.timed_out,
            cancelled=cancelled,
            capture_error=outcome.capture_error,
        )
        self.ledger.record(paths, workspace=request.workspace)
        gateway = read_gateway_outcome(request.trace_dir) if gateway_expected else None
        if gateway is not None:
            outcome = replace(
                outcome,
                provider_gateway_outcome=gateway.outcome,
                provider_gateway_request_count=gateway.request_count,
                provider_gateway_manifest=gateway.manifest,
                usage=gateway.usage,
            )
        if gateway_expected:
            self.emit(
                "provider_gateway_finished",
                "succeeded" if gateway is not None and gateway.healthy else "failed",
                scope=gateway_scope,
                data={
                    "outcome": gateway.outcome if gateway is not None else "unavailable",
                    "request_count": gateway.request_count if gateway is not None else 0,
                },
            )
        outcome = replace(
            outcome,
            native_trace=paths.stdout,
            stderr_trace=paths.stderr,
            trace_chunks=paths.chunks,
            trace_outcome=paths.outcome,
            cancelled=cancelled,
            termination_reason=(
                outcome.termination_reason
                or ("controller_cancelled" if cancelled else "adapter_exit")
            ),
        )
        self.record_usage(request, outcome)
        return outcome
