"""Use benchmark evidence without imposing a framework-wide score range."""

from collections.abc import Callable
from pathlib import Path

from optiprofiler_evolve.criteria.contracts import (
    AssessmentRequest,
    Criterion,
    CriterionResult,
    GuidanceRequest,
)
from optiprofiler_evolve.models import EvaluationResult


class PerformanceCriterion(Criterion):
    version = "performance/1"
    guidance_fields = ("fitness_source",)

    @classmethod
    def generation_guidance(cls, request: GuidanceRequest) -> str:
        source = request.options.get("fitness_source", "solver_scores")
        text = (
            "Performance: improve the solver's ability to solve the public problem set within "
            "the objective-evaluation budget. Use benchmark evidence to justify algorithmic "
            "changes, not merely a blind hyperparameter sweep. "
            f"The configured fitness source is {source}; higher reported fitness is better. "
            "Keep comparisons within the same experiment and evaluation profile."
        )
        if {"smoke", "public"}.intersection(request.profiles):
            text += " Use the performance scores returned by the enabled public checks."
        if "public" in request.profiles and request.feedback_mode == "full_artifacts":
            text += (
                " When a successful public evaluation supplies benchmark artifacts, read "
                "artifact_index.json and inspect the available aggregate profile and representative "
                "per-problem history plots before the final edit. Use render_pdf for PDF pages "
                "when provided. Record which evidence informed the change. Opaque problem IDs "
                "are stable within the run; do not try to recover their source-library names."
            )
        return text

    def __init__(self, evaluate: Callable[[AssessmentRequest], EvaluationResult]) -> None:
        self.evaluate = evaluate
        self.result: EvaluationResult | None = None

    def assess(self, request: AssessmentRequest) -> CriterionResult:
        self.result = result = self.evaluate(request)
        if not result.success:
            return CriterionResult("failed", error=result.error)
        return CriterionResult(
            "succeeded", score=result.score, satisfied=True,
            metrics={"candidate_score": result.candidate_score,
                     "reference_score": result.reference_score,
                     "problem_count": result.problem_count},
            evidence=(Path("."),),
        )
