"""Semantic review is a replaceable criterion, not an admission authority."""

from collections.abc import Callable
from pathlib import Path

from optiprofiler_evolve.criteria.contracts import (
    AssessmentRequest,
    Criterion,
    CriterionBackends,
    CriterionResult,
    GuidanceRequest,
)
from optiprofiler_evolve.criteria.integrity.reviewer import IntegrityReviewDecision


class IntegrityCriterion(Criterion):
    assessment_order = 10
    version = "integrity-review/1"

    @classmethod
    def generation_guidance(cls, request: GuidanceRequest) -> str:
        return (
            "Semantic integrity: make a genuine solver improvement. Do not recognize specific "
            "test cases, hard-code answers, fabricate measurements, or exploit the evaluation "
            "harness. Keep dependencies and claimed mechanisms explicit and auditable. "
            "An independent review scores approval as 1 and quarantine as 0. "
            "Its generation guidance is not a substitute for the separate review."
        )

    def __init__(
        self, review: Callable[[AssessmentRequest], tuple[IntegrityReviewDecision, Path]]
    ) -> None:
        self.review = review

    @classmethod
    def bind(cls, backends: CriterionBackends, *, mode: str) -> Criterion:
        return cls(backends.review)

    def assess(self, request: AssessmentRequest) -> CriterionResult:
        decision, path = self.review(request)
        approved = decision.verdict == "approve"
        return CriterionResult(
            "succeeded", score=1.0 if approved else 0.0, satisfied=approved,
            metrics={"verdict": decision.verdict, "summary": decision.summary},
            evidence=(path,), error=None if approved else "integrity_review_quarantine",
        )
