"""Seed-relative source score and a separately configurable size constraint."""

from dataclasses import asdict, dataclass
from pathlib import Path

from optiprofiler_evolve.criteria.complexity.analyzer import (
    ANALYZER,
    METRICS_VERSION,
    ComplexityChecker,
    ComplexityMetrics,
)
from optiprofiler_evolve.criteria.contracts import (
    AssessmentRequest,
    Criterion,
    CriterionResult,
    GuidanceRequest,
)
from optiprofiler_evolve.files import write_json


@dataclass(frozen=True)
class ComplexityReport:
    tree_hash: str
    baseline_hash: str
    baseline: ComplexityMetrics
    candidate: ComplexityMetrics | None
    mode: str
    max_ratio: float
    error: str | None = None
    analyzer: str = ANALYZER
    schema: str = METRICS_VERSION

    @property
    def ratios(self) -> tuple[float, float] | None:
        if self.candidate is None:
            return None
        return (
            self.candidate.decision_complexity / self.baseline.decision_complexity,
            self.candidate.logical_lines / max(self.baseline.logical_lines, 1),
        )

    @property
    def score(self) -> float | None:
        ratios = self.ratios
        return 1 / (1 + max(ratios)) if ratios is not None else None

    @property
    def status(self) -> str:
        if self.ratios is None:
            return "unavailable"
        return "within_limit" if max(self.ratios) <= self.max_ratio else "exceeded"

    @property
    def blocked(self) -> bool:
        return self.mode == "enforce" and self.status != "within_limit"

    def summary(self) -> dict[str, object]:
        ratios = self.ratios
        return {
            "mode": self.mode,
            "status": self.status,
            "max_ratio": self.max_ratio,
            "branch_ratio": ratios[0] if ratios else None,
            "size_ratio": ratios[1] if ratios else None,
            "complexity_ratio": max(ratios) if ratios else None,
            "score": self.score,
            "complexity_percent": 100 * max(ratios) if ratios else None,
            "blocked": self.blocked,
        }

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), **self.summary()}

    def worker_dict(self) -> dict[str, object]:
        # Initial source locations and hashes remain controller-private.
        return {
            **self.summary(),
            "schema": self.schema,
            "analyzer": self.analyzer,
            "baseline": {
                "decision_complexity": self.baseline.decision_complexity,
                "logical_lines": self.baseline.logical_lines,
            },
            "candidate": asdict(self.candidate) if self.candidate else None,
            "error": self.error,
        }


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

    def __init__(self, checker: ComplexityChecker, *, mode: str) -> None:
        self.checker = checker
        self.mode = mode

    def assess(self, request: AssessmentRequest) -> CriterionResult:
        error = None
        try:
            metrics = self.checker.measure(request.candidate)
        except (ValueError, OSError) as exc:
            metrics, error = None, str(exc)
        report = ComplexityReport(
            request.tree_hash, self.checker.baseline_hash, self.checker.baseline, metrics,
            self.mode, float(request.options.get("max_ratio", 1.2)), error,
        )
        write_json(request.output_dir / "complexity.json", report.as_dict())
        if report.ratios is None:
            return CriterionResult("failed", metrics=report.worker_dict(), error=report.error,
                                   evidence=(Path("complexity.json"),))
        ratio = max(report.ratios)
        return CriterionResult(
            "succeeded", score=report.score, satisfied=ratio <= report.max_ratio,
            metrics=report.worker_dict(), evidence=(Path("complexity.json"),),
            error=None if ratio <= report.max_ratio else "complexity_limit_exceeded",
        )
