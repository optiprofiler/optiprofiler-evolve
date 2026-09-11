"""Public-only capabilities bound to one private solver attempt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from optiprofiler_evolve.controller.assessment import PublicAssessment

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.runtime import RunEnvironment

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from optiprofiler_evolve.agents.broker import EvaluationBroker
from optiprofiler_evolve.agents.contracts import SolverJob, WorkerOutcome, WorkerRequest
from optiprofiler_evolve.agents.explanations import capture_solver_explanation, explanation_status
from optiprofiler_evolve.config import WorkerConfig
from optiprofiler_evolve.models import CandidateRecord, EvaluationResult
from optiprofiler_evolve.phases.attempts.contracts import AttemptCapabilities
from optiprofiler_evolve.phases.attempts.prompt import build_worker_prompt
from optiprofiler_evolve.solver import changed_files, tree_hash, validate_edit_scope


class ControllerAttemptCapabilities(AttemptCapabilities):
    """Public-only attempt services; validation and hidden are intentionally absent."""

    def __init__(
        self,
        *,
        environment: RunEnvironment,
        attempt_id: str,
        iteration: int,
        island: int,
        attempt_index: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
        workspace: Path,
    ) -> None:
        self._environment = environment
        self._attempt_id = attempt_id
        self._iteration = iteration
        self._island = island
        self._attempt_index = attempt_index
        self._parent = parent
        self._worker = worker
        self._workspace = workspace
        self.changed: tuple[str, ...] = ()
        self.worker_outcome: WorkerOutcome | None = None
        self.public_evaluation: tuple[str, EvaluationResult] | None = None
        self._evaluation_counts: dict[str, int] = {}

    def run_worker(self) -> WorkerOutcome:
        environment = self._environment
        if environment.state.assessor is None or environment.state.data is None:
            raise RuntimeError("Worker capability requires prepared evaluator and data.")
        tools_dir = environment.run_dir / "controller" / "worker_tools" / self._attempt_id
        transcript = environment.run_dir / "transcripts" / f"{self._attempt_id}.jsonl"
        broker = EvaluationBroker(
            workspace=self._workspace,
            control_dir=environment.run_dir / "controller" / "brokers" / self._attempt_id,
            evaluator=PublicAssessment(environment.state.assessor.evaluate,
                                       candidate_id=self._attempt_id, parent=self._parent.path),
            max_smoke_calls=environment.config.execution.max_smoke_calls_per_worker,
            max_public_calls=environment.config.execution.max_public_calls_per_worker,
            candidate_validator=self._ensure_candidate_safe,
            feedback_mode=environment.config.execution.feedback_mode,
            emit=lambda kind, status, data: environment.emit(
                kind, status, scope=self._scope(), data=data,
            ),
        )
        broker.install_tools(tools_dir)
        connection = broker.start(docker=environment.config.sandbox.backend == "docker")
        prompt = build_worker_prompt(
            interface=environment.interface,
            runtime=environment.runtime,
            editable=environment.editable,
            data=environment.state.data,
            iteration=self._iteration,
            island=self._island,
            parent_score=self._parent.public_score,
            controller_memory=environment.memory_for(self._island),
            token_budget=environment.config.workers.token_budget,
            max_smoke_calls=environment.config.execution.max_smoke_calls_per_worker,
            max_public_calls=environment.config.execution.max_public_calls_per_worker,
            forbidden_candidate_imports=environment.config.execution.forbidden_candidate_imports,
            direction=environment.state.direction_assignments.get(self._island),
            prompt_note=environment.config.workers.prompt_note,
        )
        try:
            environment.emit(
                "worker_started",
                "running",
                scope=self._scope(),
                data={"worker": f"{self._worker.harness}:{self._worker.model}"},
            )
            client = environment.llm_client(
                WorkerRequest(
                    worker=self._worker,
                    workers=environment.config.workers,
                    sandbox=environment.config.sandbox,
                    workspace=self._workspace,
                    tools_dir=tools_dir,
                    broker=connection,
                    prompt=prompt,
                    transcript=transcript,
                    trace_dir=environment.run_dir / "traces" / self._attempt_id,
                    trace_context={
                        "schema": "trace_input/1",
                        "join": {
                            "attempt_id": self._attempt_id,
                            "candidate_id": self._attempt_id,
                            "parent_id": self._parent.candidate_id,
                            "phase": "explore",
                            "module": "candidate_attempt",
                            "iteration": self._iteration,
                            "island": self._island,
                            "attempt_index": self._attempt_index,
                        },
                        "parent_id": self._parent.candidate_id,
                        "parent_tree_hash": self._parent.tree_hash,
                    },
                    cancellation_event=environment.cancellation_event,
                    evaluation_tools=tuple(tool for tool, limit in (
                        ("smoke_test", environment.config.execution.max_smoke_calls_per_worker),
                        ("evaluate", environment.config.execution.max_public_calls_per_worker),
                    ) if limit > 0),
                )
            )
            outcome = client.run(
                SolverJob(role="explorer", job_id=self._attempt_id, prompt=prompt)
            ).outcome
        except Exception as exc:
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                f"[controller] worker launch failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            outcome = WorkerOutcome(1, transcript)
        finally:
            broker.stop()
        explanation = capture_solver_explanation(
            self._workspace,
            environment.run_dir / "explanations" / f"{self._attempt_id}.json",
            attempt_id=self._attempt_id,
        )
        outcome = replace(outcome, solver_explanation=explanation)
        self.worker_outcome = outcome
        environment.emit(
            "worker_finished",
            ("cancelled" if outcome.cancelled else "failed" if outcome.returncode else "succeeded"),
            scope=self._scope(),
            data={
                "returncode": outcome.returncode,
                "timed_out": outcome.timed_out,
                "cancelled": outcome.cancelled,
                "termination_reason": outcome.termination_reason,
                "transcript": str(outcome.transcript),
                "native_trace": str(outcome.native_trace) if outcome.native_trace else None,
                "stderr_trace": str(outcome.stderr_trace) if outcome.stderr_trace else None,
                "trace_chunks": str(outcome.trace_chunks) if outcome.trace_chunks else None,
                "trace_outcome": str(outcome.trace_outcome) if outcome.trace_outcome else None,
                "trace_capture_error": outcome.capture_error,
                "provider_gateway_outcome": outcome.provider_gateway_outcome,
                "provider_gateway_request_count": outcome.provider_gateway_request_count,
                "provider_gateway_manifest": (
                    str(outcome.provider_gateway_manifest)
                    if outcome.provider_gateway_manifest
                    else None
                ),
                "solver_explanation": str(explanation),
                "solver_explanation_status": explanation_status(explanation),
            },
        )
        return outcome

    def audit_candidate(self) -> tuple[str, ...]:
        self._environment.validate_candidate(self._workspace)
        changed = changed_files(self._parent.path, self._workspace)
        if not changed:
            raise ValueError("Worker produced no solver changes.")
        validate_edit_scope(changed, self._environment.editable)
        self.changed = changed
        return changed

    def evaluate_public(self, mode: str) -> EvaluationResult:
        if mode not in {"smoke", "public", "public_score"}:
            raise ValueError(f"Attempt steps cannot evaluate controller-only mode {mode!r}.")
        self.audit_candidate()
        if self._environment.state.assessor is None:
            raise RuntimeError("Evaluator is unavailable.")
        directory_name = "public" if mode in {"public", "public_score"} else mode
        count = self._evaluation_counts.get(directory_name, 0) + 1
        self._evaluation_counts[directory_name] = count
        if count > 1:
            directory_name = f"{directory_name}-{count:03d}"
        evaluated_hash = tree_hash(self._workspace)
        evaluation_mode = (
            "public" if mode == "public_score"
            and self._environment.config.execution.feedback_mode == "full_artifacts"
            and any(phase.name == "strategy_analysis"
                    for phase in self._environment.config.workflow.phases) else mode
        )
        result = self._environment.state.assessor.evaluate(
            self._workspace,
            evaluation_mode,
            self._environment.run_dir / "controller" / "evaluations" / self._attempt_id / directory_name,
            candidate_id=self._attempt_id, parent=self._parent.path,
        )
        if mode in {"public", "public_score"}:
            self.public_evaluation = (evaluated_hash, result)
        return result

    def _ensure_candidate_safe(self, root: Path) -> None:
        self._environment.validate_candidate(root)
        changed = changed_files(self._parent.path, root)
        if changed:
            validate_edit_scope(changed, self._environment.editable)

    def _scope(self) -> Mapping[str, object]:
        return {
            "phase": "explore",
            "iteration": self._iteration,
            "island": self._island,
            "attempt_id": self._attempt_id,
        }
