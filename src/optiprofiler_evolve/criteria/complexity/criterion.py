"""Seed-relative source score and a separately configurable size constraint."""

from dataclasses import replace

from optiprofiler_evolve.criteria.complexity.analyzer import ComplexityChecker
from optiprofiler_evolve.criteria.contracts import (
    AssessmentRequest,
    Criterion,
    CriterionResult,
    GuidanceRequest,
)


class ComplexityCriterion(Criterion):
    version = "source_complexity/2"
    guidance_fields = ("max_ratio",)

    @classmethod
    def generation_guidance(cls, request: GuidanceRequest) -> str:
        ratio = float(request.options.get("max_ratio", 1.2))
        # Baseline worker-task wording. Absolute seed counts stay public-tool
        # evidence instead of a second analysis pass while building the prompt.
        return (
            f"Complexity budget ({request.mode}): branches and logical lines may each be at "
            f"most {ratio:g} times their initial values. All candidate Python files count. "
            "smoke_test/evaluate return diagnostics. Simplify the algorithm rather than "
            "compress formatting or remove documentation. In warn mode this is advice only, "
            "not grounds for semantic quarantine. In enforce mode an exceeded or unavailable "
            "analysis blocks evaluation and admission."
        )

    def __init__(self, checker: ComplexityChecker) -> None:
        self.checker = checker

    def assess(self, request: AssessmentRequest) -> CriterionResult:
        report = replace(self.checker.assess(request.candidate),
                         max_ratio=float(request.options.get("max_ratio", 1.2)))
        if report.ratios is None:
            return CriterionResult("failed", error=report.error)
        ratio = max(report.ratios)
        return CriterionResult(
            "succeeded", score=report.score, satisfied=ratio <= report.max_ratio,
            metrics=report.worker_dict(),
            error=None if ratio <= report.max_ratio else "complexity_limit_exceeded",
        )
