"""Private attempt execution, returning records for controller admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.runtime import RunEnvironment

from optiprofiler_evolve.agents.contracts import WorkerOutcome
from optiprofiler_evolve.agents.feedback import publish_feedback
from optiprofiler_evolve.agents.jobs import guidance_id as _guidance_id
from optiprofiler_evolve.config import WorkerConfig
from optiprofiler_evolve.models import Attempt, CandidateRecord, EvaluationResult
from optiprofiler_evolve.observability.provenance import coordinate_seed
from optiprofiler_evolve.phases.attempts.capabilities import (
    ControllerAttemptCapabilities as _ControllerAttemptCapabilities,
)
from optiprofiler_evolve.phases.attempts.contracts import (
    AttemptContext,
    StepResult,
)
from optiprofiler_evolve.solver import copy_solver_tree, tree_hash


class AttemptRunner:
    """Execute private attempts; only the environment admits returned records."""

    def __init__(self, environment: RunEnvironment) -> None:
        self.environment = environment

    def run(
        self,
        *,
        iteration: int,
        island: int,
        attempt_index: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
    ) -> Attempt:
        self.environment.commits.require_open_selection()
        attempt_id = f"it{iteration:03d}-i{island:02d}-a{attempt_index:02d}"
        scope = {
            "phase": "explore",
            "iteration": iteration,
            "island": island,
            "attempt_id": attempt_id,
        }
        self.environment.emit(
            "attempt_started",
            "running",
            scope=scope,
            data={
                "parent_id": parent.candidate_id,
                "worker": f"{worker.harness}:{worker.model}",
                "guidance": _guidance_id(self.environment.state.direction_assignments.get(island)),
            },
        )
        workspace = self.environment.run_dir / "workspaces" / attempt_id
        copy_solver_tree(parent.path, workspace)
        capabilities = _ControllerAttemptCapabilities(
            environment=self.environment,
            attempt_id=attempt_id,
            iteration=iteration,
            island=island,
            attempt_index=attempt_index,
            parent=parent,
            worker=worker,
            workspace=workspace,
        )
        results: list[StepResult] = []
        rejected = False
        for step_index, step in enumerate(self.environment.attempt_steps):
            step_scope = {**scope, "step": step.name, "step_idx": step_index}
            self.environment.emit("step_started", "running", scope=step_scope)
            context = AttemptContext(
                attempt_id=attempt_id,
                iteration=iteration,
                island=island,
                attempt_index=attempt_index,
                parent_id=parent.candidate_id,
                parent_score=parent.public_score,
                workspace=workspace,
                worker_name=f"{worker.harness}:{worker.model}",
                rng_seed=coordinate_seed(
                    self.environment.config.evolution.random_seed,
                    "explore",
                    iteration,
                    island,
                    attempt_index,
                    step.name,
                ),
                prior_results=tuple(results),
                capabilities=capabilities,
                emit=self.environment.scoped_emitter(step_scope),
            )
            try:
                result = step.run(context)
            except Exception as exc:
                result = StepResult(
                    verdict="reject",
                    error=f"{type(exc).__name__}: {exc}",
                )
            # Process completion and candidate validity are separate signals.
            # A CLI can return nonzero after writing a complete solver (for
            # example when its model budget is exhausted). The remaining
            # controller-owned gates decide whether that filesystem snapshot
            # is safe and complete enough to admit.
            results.append(result)
            status = "failed" if result.verdict == "reject" else "succeeded"
            self.environment.emit(
                "step_finished",
                status,
                scope=step_scope,
                data={
                    "verdict": result.verdict,
                    "metrics": dict(result.metrics),
                    "artifacts": list(result.artifacts),
                    "error": result.error,
                },
            )
            if result.verdict == "reject":
                rejected = True
                break

        changed = capabilities.changed
        outcome = capabilities.worker_outcome or WorkerOutcome(
            1,
            self.environment.run_dir / "transcripts" / f"{attempt_id}.jsonl",
        )
        metrics = {key: value for result in results for key, value in result.metrics.items()}
        validation: EvaluationResult | None = None
        try:
            if rejected:
                error = next((result.error for result in reversed(results) if result.error), None)
                raise RuntimeError(error or "attempt pipeline rejected candidate")
            # Non-removable safety and controller-only validation gate.
            changed = capabilities.audit_candidate()
            public = capabilities.public_evaluation
            if public is None or public[0] != tree_hash(workspace):
                public_result = capabilities.evaluate_public("public_score")
            else:
                public_result = public[1]
            if not public_result.success:
                raise RuntimeError(public_result.error or "public evaluation failed")
            public_score = public_result.score
            metrics["public_score"] = public_score
            if self.environment.state.assessor is None:
                raise RuntimeError("Evaluator is unavailable.")
            validation = self.environment.state.assessor.evaluate(
                workspace,
                "validation",
                self.environment.run_dir / "controller" / "evaluations" / attempt_id / "validation",
                candidate_id=attempt_id, parent=parent.path,
                mutation_transcript=outcome.transcript, mutation_worker=worker,
            )
            if not validation.success:
                raise RuntimeError(validation.error or "controller validation evaluation failed")
            feedback = publish_feedback(
                public_result, self.environment.run_dir / "controller" / "feedback" / attempt_id,
                mode=self.environment.config.execution.feedback_mode,
                emit=self.environment.scoped_emitter(scope),
            )
            snapshot = self.environment.run_dir / "candidates" / attempt_id
            copy_solver_tree(workspace, snapshot)
            record = CandidateRecord(
                candidate_id=attempt_id,
                island=island,
                iteration=iteration,
                attempt_index=attempt_index,
                parent_id=parent.candidate_id,
                path=snapshot,
                tree_hash=tree_hash(snapshot),
                public_score=public_score,
                validation_score=validation.score,
                selection_metrics=validation.metric_bundle,
                assessment_score=validation.assessment_score,
                assessment=validation.assessment,
                public_evidence=feedback.output_dir,
                worker=f"{worker.harness}:{worker.model}",
                guidance=_guidance_id(self.environment.state.direction_assignments.get(island)),
            )
        except Exception as exc:
            record = CandidateRecord(
                candidate_id=attempt_id,
                island=island,
                iteration=iteration,
                attempt_index=attempt_index,
                parent_id=parent.candidate_id,
                path=workspace,
                tree_hash="",
                public_score=metrics.get("public_score"),
                validation_score=validation.score if validation else None,
                assessment=validation.assessment if validation else None,
                worker=f"{worker.harness}:{worker.model}",
                guidance=_guidance_id(self.environment.state.direction_assignments.get(island)),
                valid=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        evaluated = capabilities.public_evaluation is not None
        return Attempt(record, changed, outcome, tuple(results), evaluated)

    def failed(
        self,
        iteration: int,
        island: int,
        attempt_index: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
        exc: Exception,
    ) -> Attempt:
        attempt_id = f"it{iteration:03d}-i{island:02d}-a{attempt_index:02d}"
        transcript = self.environment.run_dir / "transcripts" / f"{attempt_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            f"[controller] attempt setup failed: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        record = CandidateRecord(
            candidate_id=attempt_id,
            island=island,
            iteration=iteration,
            attempt_index=attempt_index,
            parent_id=parent.candidate_id,
            path=self.environment.run_dir / "workspaces" / attempt_id,
            tree_hash="",
            public_score=None,
            validation_score=None,
            worker=f"{worker.harness}:{worker.model}",
            guidance=_guidance_id(self.environment.state.direction_assignments.get(island)),
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        return Attempt(record, (), WorkerOutcome(1, transcript), ())
