"""Bind quality criteria to controller-owned execution capabilities."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from optiprofiler_evolve.config import EvolveConfig, WorkerConfig
from optiprofiler_evolve.criteria.complexity.analyzer import ComplexityChecker
from optiprofiler_evolve.criteria.contracts import AssessmentRequest, Criterion, CriterionBackends
from optiprofiler_evolve.criteria.performance.contracts import Evaluator
from optiprofiler_evolve.criteria.runner import AssessmentRunner
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.solver import InterfaceSpec, changed_files, tree_hash

if TYPE_CHECKING:
    from optiprofiler_evolve.criteria.integrity.reviewer import IntegrityReviewDecision


class CandidateAssessor:
    """Shared entry for public tools, admission, research, and the final holdout."""

    name = "assessment"
    deterministic = False

    def __init__(
        self, *, config: EvolveConfig, seed: Path, reference: Path | None,
        data: DataPlan, runtime: str, interface: InterfaceSpec, run_dir: Path,
        evaluator_factory: Callable[..., Evaluator],
        criterion_factories: Mapping[str, type[Criterion]],
        review: Callable[..., tuple[IntegrityReviewDecision, Path]],
    ) -> None:
        self.config = config
        self.seed = seed
        self.reference = reference
        self.data = data
        self.runtime = runtime
        self.interface = interface
        self.run_dir = run_dir
        self.evaluator_factory = evaluator_factory
        self.criterion_factories = dict(criterion_factories)
        self.review = review
        self._evaluators: dict[tuple[str, Path | None], Evaluator] = {}
        self._evaluator_lock = Lock()
        self.input_fingerprint = hashlib.sha256(
            f"{tree_hash(seed)}:{data.split_fingerprint}:{interface}:{runtime}".encode()
        ).hexdigest()
        self.complexity = None
        if config.assessment.enabled("complexity"):
            if runtime != "python":
                raise ValueError("Complexity assessment supports Python solvers only.")
            self.complexity = ComplexityChecker(
                seed, config.sandbox, run_dir / "controller" / "complexity"
            )

    def evaluate(
        self, candidate: Path, mode: str, output_dir: Path, *,
        candidate_id: str | None = None, parent: Path | None = None,
        mutation_transcript: Path | None = None, mutation_worker: WorkerConfig | None = None,
        reference: Path | None = None,
    ) -> EvaluationResult:
        profile = "public" if mode in {"public", "public_score"} else mode
        resolved = self.config.resolved_profiles[profile]
        specs = resolved.criteria
        if profile == "hidden" and not self.data.hidden and "performance" in specs:
            if self.data.lockbox_required and specs["performance"].enabled:
                raise ValueError("required lockbox has no hidden problems")
            specs = {**specs, "performance": replace(specs["performance"], enabled=False)}

        performance_result = None

        def benchmark(request: AssessmentRequest) -> EvaluationResult:
            nonlocal performance_result
            key = (profile, reference or self.reference)
            with self._evaluator_lock:
                if key not in self._evaluators:
                    self._evaluators[key] = self.evaluator_factory(
                        runtime=self.runtime, reference=key[1], interface=self.interface,
                        data=self.data, config=resolved.performance,
                    )
                evaluator = self._evaluators[key]
            performance_result = evaluator.evaluate(request.candidate, mode, request.output_dir.parent)
            return performance_result

        def review(request: AssessmentRequest):
            base = parent or self.seed
            return self.review(
                candidate_id=candidate_id or candidate.name, candidate=request.candidate,
                parent=base, changed=changed_files(base, request.candidate),
                mutation_transcript=mutation_transcript, mutation_worker=mutation_worker,
                profile=profile,
            )

        # Bind inexpensive/source constraints before benchmark execution.
        backends = CriterionBackends(benchmark, review, self.complexity)
        criteria = {}
        enabled = [name for name, spec in specs.items() if spec.enabled]
        for name in sorted(enabled, key=lambda name: self.criterion_factories[name].assessment_order):
            criteria[name] = self.criterion_factories[name].bind(backends, mode=specs[name].mode)
        report = AssessmentRunner(criteria).run(
            AssessmentRequest(candidate, tree_hash(candidate), profile, output_dir,
                              input_fingerprint=self.input_fingerprint), specs
        )
        raw = performance_result
        if raw is None:
            raw = EvaluationResult(mode, None, None, None, 0, output_dir)
        error = None
        if not report.accepted:
            error = "; ".join(
                f"{name}: {item.error or item.status}" for name, item in report.results.items()
                if report.criteria[name]["enabled"] and (
                    item.status != "succeeded" or not item.satisfied
                )
            ) or "Assessment has no complete weighted score."
        return replace(raw, output_dir=output_dir, success=report.accepted,
                       error=error, assessment_score=report.total, assessment=report.as_dict())


class PublicAssessment:
    """Broker adapter that cannot request controller-only assessment profiles."""

    name = "public_assessment"
    deterministic = False

    def __init__(self, assess: Callable[..., EvaluationResult], **metadata: object) -> None:
        self._assess = assess
        self._metadata = metadata

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        if mode not in {"smoke", "public", "public_score"}:
            raise ValueError("Worker assessment is public-only.")
        return self._assess(candidate, mode, output_dir, **self._metadata)
