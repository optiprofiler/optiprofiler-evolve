"""Seed-relative source score and a separately configurable size constraint."""

from dataclasses import replace

from optiprofiler_evolve.criteria.complexity.analyzer import ComplexityChecker
from optiprofiler_evolve.criteria.contracts import AssessmentRequest, Criterion, CriterionResult, GuidanceRequest


class ComplexityCriterion(Criterion):
    version = "source_complexity/1"
    guidance_fields = ("max_ratio",)

    @classmethod
    def generation_guidance(cls, request: GuidanceRequest) -> str:
        ratio = float(request.options.get("max_ratio", 1.2))
        return (
            f"Complexity budget ({request.mode}): branches and logical lines may each be at most "
            f"{ratio:g} times their initial values. All candidate Python files count. "
            "The score is 1 / (1 + r), where r is the larger initial-relative ratio. "
            "Simplify the algorithm rather than compress formatting or remove documentation. "
            "Enabled public checks provide numeric baseline totals and candidate diagnostics; "
            "use those to locate avoidable branches and duplication."
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
            "succeeded", score=1 / (1 + ratio), satisfied=ratio <= report.max_ratio,
            metrics=report.worker_dict(),
            error=None if ratio <= report.max_ratio else "complexity_limit_exceeded",
        )
