"""Default one-way candidate attempt pipeline."""

from __future__ import annotations

from ..protocols import AttemptContext, StepResult


class MutateStep:
    name = "mutate"

    def run(self, context: AttemptContext) -> StepResult:
        outcome = context.capabilities.run_worker()
        return StepResult(
            metrics={
                "worker_returncode": outcome.returncode,
                "worker_timed_out": outcome.timed_out,
            },
            artifacts=(str(outcome.transcript),),
        )


class StaticAuditStep:
    name = "static_audit"

    def run(self, context: AttemptContext) -> StepResult:
        changed = context.capabilities.audit_candidate()
        return StepResult(metrics={"changed_files": list(changed)})


class SmokeStep:
    name = "smoke"

    def run(self, context: AttemptContext) -> StepResult:
        result = context.capabilities.evaluate_public("smoke")
        return StepResult(
            verdict="pass" if result.success else "reject",
            metrics={
                "smoke_score": result.score,
                "smoke_success": result.success,
            },
            artifacts=(str(result.output_dir),),
            error=result.error,
        )


class PublicEvaluateStep:
    name = "public_evaluate"

    def run(self, context: AttemptContext) -> StepResult:
        result = context.capabilities.evaluate_public("public_score")
        return StepResult(
            verdict="pass" if result.success else "reject",
            metrics={
                "public_score": result.score,
                "candidate_score": result.candidate_score,
                "reference_score": result.reference_score,
                "public_success": result.success,
            },
            artifacts=(str(result.output_dir),),
            error=result.error,
        )


class FeedbackStep:
    name = "feedback"

    def run(self, context: AttemptContext) -> StepResult:
        artifacts = tuple(
            artifact for result in context.prior_results for artifact in result.artifacts
        )
        return StepResult(
            metrics={"evidence_artifact_count": len(artifacts)},
            artifacts=artifacts,
        )


__all__: list[str] = []
