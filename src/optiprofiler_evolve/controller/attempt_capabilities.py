"""Public-only capabilities bound to one private solver attempt."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import EvolutionEngine

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from optiprofiler_evolve.evaluation.broker import EvaluationBroker
from optiprofiler_evolve.phases.explore.prompt import build_worker_prompt
from optiprofiler_evolve.shared.config import WorkerConfig
from optiprofiler_evolve.shared.explanations import (
    capture_solver_explanation,
    explanation_status,
)
from optiprofiler_evolve.shared.models import CandidateRecord, EvaluationResult
from optiprofiler_evolve.shared.protocols import (
    AgentJob,
    AttemptCapabilities,
    WorkerOutcome,
    WorkerRequest,
)
from optiprofiler_evolve.shared.solver import (
    changed_files,
    tree_hash,
    validate_edit_scope,
)


class EngineAttemptCapabilities(AttemptCapabilities):
    """Public-only attempt services; validation and hidden are intentionally absent."""

    def __init__(
        self,
        *,
        engine: EvolutionEngine,
        attempt_id: str,
        iteration: int,
        island: int,
        attempt_index: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
        workspace: Path,
    ) -> None:
        self._engine = engine
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
        engine = self._engine
        if engine.evaluator is None or engine.data is None:
            raise RuntimeError("Worker capability requires prepared evaluator and data.")
        tools_dir = engine.run_dir / "controller" / "worker_tools" / self._attempt_id
        transcript = engine.run_dir / "transcripts" / f"{self._attempt_id}.jsonl"
        broker = EvaluationBroker(
            workspace=self._workspace,
            control_dir=engine.run_dir / "controller" / "brokers" / self._attempt_id,
            evaluator=engine.evaluator,
            max_smoke_calls=engine.config.evaluation.max_smoke_calls_per_worker,
            max_public_calls=engine.config.evaluation.max_public_calls_per_worker,
            candidate_validator=self._ensure_candidate_safe,
            feedback_mode=engine.config.evaluation.feedback_mode,
            complexity_checker=(lambda root: engine._assess_complexity(root, self._scope()))
            if engine.complexity is not None
            else None,
        )
        broker.install_tools(tools_dir)
        connection = broker.start(docker=engine.config.sandbox.backend == "docker")
        prompt = build_worker_prompt(
            interface=engine.interface,
            runtime=engine.runtime,
            editable=engine.editable,
            data=engine.data,
            iteration=self._iteration,
            island=self._island,
            parent_score=self._parent.public_score,
            controller_memory=engine._memory_for(self._island),
            token_budget=engine.config.workers.token_budget,
            max_smoke_calls=engine.config.evaluation.max_smoke_calls_per_worker,
            max_public_calls=engine.config.evaluation.max_public_calls_per_worker,
            forbidden_candidate_imports=engine.config.evaluation.forbidden_candidate_imports,
            direction=engine.direction_assignments.get(self._island),
            prompt_note=engine.config.workers.prompt_note,
            complexity_note=(
                engine.complexity.prompt()
                + "\nCurrent parent: "
                + engine.complexity.assess(self._parent.path).feedback()
            )
            if engine.complexity
            else None,
        )
        try:
            engine._emit(
                "worker_started",
                "running",
                scope=self._scope(),
                data={"worker": f"{self._worker.harness}:{self._worker.model}"},
            )
            client = engine._llm_client(
                WorkerRequest(
                    worker=self._worker,
                    workers=engine.config.workers,
                    sandbox=engine.config.sandbox,
                    workspace=self._workspace,
                    tools_dir=tools_dir,
                    broker=connection,
                    prompt=prompt,
                    transcript=transcript,
                    trace_dir=engine.run_dir / "traces" / self._attempt_id,
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
                    cancellation_event=engine._cancellation_event,
                )
            )
            outcome = client.run(
                AgentJob(role="explorer", job_id=self._attempt_id, prompt=prompt)
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
            engine.run_dir / "explanations" / f"{self._attempt_id}.json",
            attempt_id=self._attempt_id,
        )
        outcome = replace(outcome, solver_explanation=explanation)
        self.worker_outcome = outcome
        engine._emit(
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
        self._engine._validate_candidate(self._workspace)
        changed = changed_files(self._parent.path, self._workspace)
        if not changed:
            raise ValueError("Worker produced no solver changes.")
        validate_edit_scope(changed, self._engine.editable)
        self.changed = changed
        return changed

    def evaluate_public(self, mode: str) -> EvaluationResult:
        if mode not in {"smoke", "public", "public_score"}:
            raise ValueError(f"Attempt steps cannot evaluate controller-only mode {mode!r}.")
        self.audit_candidate()
        self._engine._check_complexity(self._workspace, self._scope())
        if self._engine.evaluator is None:
            raise RuntimeError("Evaluator is unavailable.")
        directory_name = "public" if mode in {"public", "public_score"} else mode
        count = self._evaluation_counts.get(directory_name, 0) + 1
        self._evaluation_counts[directory_name] = count
        if count > 1:
            directory_name = f"{directory_name}-{count:03d}"
        evaluated_hash = tree_hash(self._workspace)
        result = self._engine.evaluator.evaluate(
            self._workspace,
            mode,
            self._engine.run_dir / "controller" / "evaluations" / self._attempt_id / directory_name,
        )
        if mode == "public_score":
            self.public_evaluation = (evaluated_hash, result)
        return result

    def _ensure_candidate_safe(self, root: Path) -> None:
        self._engine._validate_candidate(root)
        changed = changed_files(self._parent.path, root)
        if changed:
            validate_edit_scope(changed, self._engine.editable)

    def _scope(self) -> Mapping[str, object]:
        return {
            "phase": "explore",
            "iteration": self._iteration,
            "island": self._island,
            "attempt_id": self._attempt_id,
        }
