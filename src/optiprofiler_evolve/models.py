"""Small immutable data models shared by the controller."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class MetricValue:
    """One named, directed metric in a controller-owned evaluation bundle."""

    name: str
    value: float | None
    direction: str
    family: str
    scope: str = "aggregate"
    valid: bool = True
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.family:
            raise ValueError("Metric name and family must be nonempty.")
        if self.direction not in {"max", "min"}:
            raise ValueError("Metric direction must be 'max' or 'min'.")
        if self.scope not in {"aggregate", "instance"}:
            raise ValueError("Metric scope must be 'aggregate' or 'instance'.")
        if self.valid:
            if self.value is None or not math.isfinite(float(self.value)):
                raise ValueError(f"Valid metric {self.name!r} must be finite.")
            if self.missing_reason is not None:
                raise ValueError("A valid metric cannot have missing_reason.")
        elif not self.missing_reason:
            raise ValueError("An invalid metric must explain missing_reason.")

    @property
    def descriptor(self) -> tuple[str, str, str, str]:
        return self.name, self.family, self.direction, self.scope


@dataclasses.dataclass(frozen=True)
class InstanceMetrics:
    """Metrics attached to one opaque controller-only problem instance."""

    instance_id: str
    metrics: tuple[MetricValue, ...]

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ValueError("instance_id must be nonempty.")
        _validate_unique_metric_names(self.metrics, f"instance {self.instance_id!r}")
        if any(metric.scope != "instance" for metric in self.metrics):
            raise ValueError("Instance metrics must declare scope='instance'.")


@dataclasses.dataclass(frozen=True)
class MetricBundle:
    """Comparable controller evidence for retention and sampling policies."""

    primary: str
    invariants_hash: str
    metrics: tuple[MetricValue, ...]
    instances: tuple[InstanceMetrics, ...] = ()
    provenance: tuple[tuple[str, str], ...] = ()
    metric_set_id: str = ""
    schema: str = "metric_bundle/1"

    def __post_init__(self) -> None:
        if self.schema != "metric_bundle/1":
            raise ValueError(f"Unsupported metric bundle schema: {self.schema!r}")
        if not self.invariants_hash:
            raise ValueError("Metric bundle invariants_hash must be nonempty.")
        _validate_unique_metric_names(self.metrics, "aggregate bundle")
        primary = self.metric(self.primary)
        if not primary.valid or primary.value is None:
            raise ValueError("The primary metric must be valid and finite.")
        instance_ids = [item.instance_id for item in self.instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("Metric bundle contains duplicate instance ids.")
        descriptors = {metric.descriptor for metric in self.metrics}
        descriptors.update(
            metric.descriptor for instance in self.instances for metric in instance.metrics
        )
        encoded = json.dumps(sorted(descriptors), separators=(",", ":"))
        expected = hashlib.sha256(encoded.encode()).hexdigest()
        if self.metric_set_id and self.metric_set_id != expected:
            raise ValueError("Metric bundle metric_set_id does not match its descriptors.")
        object.__setattr__(self, "metric_set_id", expected)

    def metric(self, name: str) -> MetricValue:
        try:
            return next(metric for metric in self.metrics if metric.name == name)
        except StopIteration as exc:
            raise KeyError(f"Metric bundle has no aggregate metric {name!r}.") from exc

    def assert_comparable(self, other: MetricBundle) -> None:
        if (
            self.metric_set_id != other.metric_set_id
            or self.invariants_hash != other.invariants_hash
        ):
            raise ValueError("Metric bundles have incompatible metric sets or invariants.")

    @classmethod
    def scalar(
        cls,
        value: float,
        *,
        invariants_hash: str,
        name: str = "fitness",
        family: str = "normalized_score",
        provenance: tuple[tuple[str, str], ...] = (),
    ) -> MetricBundle:
        return cls(
            primary=name,
            invariants_hash=invariants_hash,
            metrics=(MetricValue(name, value, "max", family),),
            provenance=provenance,
        )


def _validate_unique_metric_names(metrics: tuple[MetricValue, ...], label: str) -> None:
    names = [metric.name for metric in metrics]
    if not names:
        raise ValueError(f"{label} must contain at least one metric.")
    if len(names) != len(set(names)):
        raise ValueError(f"{label} contains duplicate metric names.")


@dataclasses.dataclass(frozen=True)
class EvaluationResult:
    """Canonical result of one candidate evaluation."""

    mode: str
    score: float
    candidate_score: float
    reference_score: float
    problem_count: int
    output_dir: Path
    success: bool = True
    error: str | None = None
    profile_scores: Any = None
    metric_bundle: MetricBundle | None = None

    def __post_init__(self) -> None:
        bundle = self.metric_bundle or MetricBundle.scalar(
            self.score,
            invariants_hash=f"legacy:{self.mode}",
            provenance=(("mode", self.mode),),
        )
        primary = bundle.metric(bundle.primary)
        if primary.value is None or not math.isclose(
            float(primary.value), float(self.score), rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("EvaluationResult.score must equal the bundle primary metric.")
        object.__setattr__(self, "metric_bundle", bundle)

    def as_dict(
        self,
        *,
        include_profile_scores: bool = False,
        include_metric_bundle: bool = False,
    ) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["output_dir"] = str(self.output_dir)
        if not include_profile_scores:
            value.pop("profile_scores", None)
        if not include_metric_bundle:
            value.pop("metric_bundle", None)
        return value


@dataclasses.dataclass(frozen=True)
class CandidateRecord:
    """One immutable solver snapshot in the population database.

    ``island`` is the attempt's origin island. Current population membership is
    represented by the outer island position in ``IterationView.populations``;
    migration does not rewrite candidate lineage.
    """

    candidate_id: str
    island: int
    iteration: int
    attempt_index: int
    parent_id: str | None
    path: Path
    tree_hash: str
    public_score: float
    validation_score: float
    selection_metrics: MetricBundle | None = None
    worker: str | None = None
    guidance: str | None = None
    valid: bool = True
    error: str | None = None

    @property
    def attempt_id(self) -> str:
        return self.candidate_id

    @property
    def generation(self) -> int:
        """Compatibility alias for older internal reports."""

        return self.iteration

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["path"] = str(self.path)
        return value


@dataclasses.dataclass(frozen=True)
class FinalistResult:
    """Controller-only final evaluation for one fixed finalist."""

    candidate_id: str
    island: int
    public_score: float
    validation_score: float
    final_score: float
    output_dir: Path
    success: bool = True
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["output_dir"] = str(self.output_dir)
        return value


@dataclasses.dataclass(frozen=True)
class EvolveResult:
    """Return value of :func:`optiprofiler_evolve.evolve`."""

    run_dir: Path
    best_solver: Path
    best_candidate_id: str
    public_score: float
    validation_score: float
    final_score: float
    finalists: tuple[FinalistResult, ...]


__all__: list[str] = []
