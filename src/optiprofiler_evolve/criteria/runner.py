"""Uniform criterion execution, constraint handling, and weighted scoring."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace

from optiprofiler_evolve.config import CriterionConfig, _freeze_value, plain_data
from optiprofiler_evolve.criteria.contracts import AssessmentRequest, Criterion, CriterionResult
from optiprofiler_evolve.errors import RunCancelled
from optiprofiler_evolve.files import write_json
from optiprofiler_evolve.solver import tree_hash


@dataclass(frozen=True)
class AssessmentResult:
    profile: str
    tree_hash: str
    config_hash: str
    results: Mapping[str, CriterionResult]
    total: float | None
    accepted: bool
    criteria: Mapping[str, Mapping[str, object]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", _freeze_value(self.results))
        object.__setattr__(self, "criteria", _freeze_value(self.criteria))

    def as_dict(self) -> dict[str, object]:
        return {"schema": "assessment/1", **plain_data(self)}


class ScoreAggregator:
    """Weights have no implied score scale; missing evidence is not zero."""

    @staticmethod
    def aggregate(
        results: Mapping[str, CriterionResult], config: Mapping[str, CriterionConfig]
    ) -> tuple[float | None, bool]:
        weighted: list[tuple[float, float]] = []
        accepted = True
        incomplete = False
        for name, spec in config.items():
            if not spec.enabled:
                continue
            result = results[name]
            if spec.mode == "enforce" and (
                result.status != "succeeded" or not result.satisfied
            ):
                accepted = False
            if spec.weight > 0:
                if result.status != "succeeded" or result.score is None:
                    incomplete = True
                else:
                    weighted.append((spec.weight, result.score))
        if incomplete:
            return None, False
        if not weighted:
            return None, accepted
        # Scale weights before summing so valid large weights cannot overflow.
        scale = max(weight for weight, _ in weighted)
        denominator = math.fsum(weight / scale for weight, _ in weighted)
        total = math.fsum((weight / scale / denominator) * score for weight, score in weighted)
        if not math.isfinite(total):
            raise ValueError("Weighted assessment score is not finite.")
        return total, accepted


class AssessmentRunner:
    def __init__(self, criteria: Mapping[str, Criterion]) -> None:
        self.criteria = dict(criteria)

    def run(
        self, request: AssessmentRequest, config: Mapping[str, CriterionConfig]
    ) -> AssessmentResult:
        signature = {
            "criteria": plain_data(config),
            "versions": {name: criterion.version for name, criterion in self.criteria.items()},
            "profile": request.profile,
            "inputs": request.input_fingerprint,
        }
        digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        results: dict[str, CriterionResult] = {}
        blocked = False
        source_valid = True
        # Binding order is controller-owned: inexpensive/source constraints precede execution.
        for name in dict.fromkeys((*self.criteria, *config)):
            if name not in config:
                continue
            spec = config[name]
            if not spec.enabled or blocked:
                results[name] = CriterionResult(
                    "skipped", error="disabled" if not spec.enabled else "prior constraint failed"
                )
                continue
            try:
                source_valid = tree_hash(request.candidate) == request.tree_hash
                if not source_valid:
                    raise ValueError("Candidate changed before assessment.")
                result = self.criteria[name].assess(replace(
                    request, options=spec.options, output_dir=request.output_dir / name
                ))
            except RunCancelled:
                raise
            except Exception as exc:
                result = CriterionResult("failed", error=f"{type(exc).__name__}: {exc}")
            # Source immutability is mandatory even for advisory, zero-weight criteria.
            source_valid = source_valid and tree_hash(request.candidate) == request.tree_hash
            if not source_valid:
                result = CriterionResult("failed", error="Candidate changed during assessment.")
            results[name] = result
            blocked = not source_valid or spec.mode == "enforce" and (
                result.status != "succeeded" or not result.satisfied
            )
        total, accepted = ScoreAggregator.aggregate(results, config)
        if not source_valid:
            total, accepted = None, False
        settings = {
            name: {"enabled": spec.enabled, "weight": spec.weight, "mode": spec.mode,
                   "version": self.criteria[name].version if name in self.criteria else None}
            for name, spec in config.items()
        }
        report = AssessmentResult(request.profile, request.tree_hash, digest, results, total, accepted, settings)
        write_json(request.output_dir / "assessment.json", report.as_dict())
        return report
