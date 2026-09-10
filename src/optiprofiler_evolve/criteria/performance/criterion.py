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

_ARTIFACT_GUIDANCE = """
- In agent-feedback experiments, inspect artifact_index.json and then open the useful
  benchmark logs, profile plots, and per-problem history plots. Use `render_pdf` to convert
  selected PDF pages to PNG, then open the PNG with the harness Read tool. At minimum, after
  a successful `evaluate`, inspect one aggregate profile and two representative per-problem
  history plots before your final edit. Record in your final response which files you read
  and what algorithmic decision each one informed. Opaque problem IDs are stable within the
  run, but their source-library names are intentionally unavailable."""


class PerformanceCriterion(Criterion):
    version = "performance/1"
    guidance_fields = ("fitness_source",)

    @classmethod
    def generation_guidance(cls, request: GuidanceRequest) -> str:
        source = request.options.get("fitness_source", "solver_scores")
        text = (
            "Performance: improve the solver's ability to solve the public problem set within "
            "the objective-evaluation budget. Use benchmark evidence to justify algorithmic "
            "changes. "
            f"The configured fitness source is {source}; higher reported fitness is better. "
            "Keep comparisons within the same experiment and evaluation profile."
        )
        if {"smoke", "public"}.intersection(request.profiles):
            text += " Use the performance scores returned by the enabled public checks."
            # Baseline worker-task wording: it is included whenever public performance
            # checks exist, and its own "In agent-feedback experiments" clause scopes it
            # to runs that publish benchmark artifacts.
            text += _ARTIFACT_GUIDANCE
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
