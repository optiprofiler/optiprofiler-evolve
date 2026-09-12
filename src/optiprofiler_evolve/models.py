"""Small immutable data models shared by the controller."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from optiprofiler_evolve.config import FrozenDict

if TYPE_CHECKING:
    from optiprofiler_evolve.agents.contracts import WorkerOutcome
    from optiprofiler_evolve.phases.attempts.contracts import StepResult


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
    score: float | None
    candidate_score: float | None
    reference_score: float | None
    problem_count: int
    output_dir: Path
    success: bool = True
    error: str | None = None
    profile_scores: Any = None
    metric_bundle: MetricBundle | None = None
    assessment_score: float | None = None
    assessment: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.score is None:
            if self.metric_bundle is not None:
                raise ValueError("Unevaluated performance cannot have a metric bundle.")
            return
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
    public_score: float | None
    validation_score: float | None
    selection_metrics: MetricBundle | None = None
    worker: str | None = None
    guidance: str | None = None
    valid: bool = True
    error: str | None = None
    assessment_score: float | None = None
    assessment: dict[str, Any] | None = None
    public_evidence: Path | None = None

    @property
    def ranking_score(self) -> float:
        """Score used for ordering, with a missing score sinking the candidate.

        Callers order archives and pick champions with this. A candidate that has
        no formal assessment score cannot be ranked, and returning negative
        infinity matches how a missing public score is already treated in
        :attr:`ranking_key`. Raising here instead turned an unrankable candidate
        into an outright crash of the run.
        """

        return self.assessment_score if self.assessment_score is not None else float("-inf")

    @property
    def ranking_key(self) -> tuple[float, float, int, str]:
        """The single ordering key for archives, validation selection and champions.

        Kept in one place because it is written by several call sites; when it was
        duplicated they could drift apart independently.
        """

        return (
            self.ranking_score,
            self.public_score if self.public_score is not None else float("-inf"),
            self.iteration,
            self.candidate_id,
        )

    @property
    def attempt_id(self) -> str:
        return self.candidate_id

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["path"] = str(self.path)
        value["public_evidence"] = str(self.public_evidence) if self.public_evidence else None
        return value


@dataclasses.dataclass(frozen=True)
class FinalistResult:
    """Controller-only final evaluation for one fixed finalist."""

    candidate_id: str
    island: int
    public_score: float | None
    validation_score: float | None
    final_score: float | None
    output_dir: Path
    success: bool = True
    error: str | None = None
    assessment_score: float | None = None
    final_assessment_score: float | None = None
    assessment_status: str = "succeeded"

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["output_dir"] = str(self.output_dir)
        return value


@dataclasses.dataclass(frozen=True)
class EvolveResult:
    """Return value of :func:`optiprofiler_evolve.evolve`."""

    run_dir: Path
    best_solver: Path | None
    best_candidate_id: str | None
    public_score: float | None
    validation_score: float | None
    final_score: float | None
    finalists: tuple[FinalistResult, ...]
    assessment_score: float | None = None
    final_assessment_score: float | None = None

    artifacts: Mapping[str, object] = dataclasses.field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", FrozenDict(self.artifacts))


@dataclasses.dataclass(frozen=True)
class Attempt:
    """Private execution result proposed to the controller for admission."""

    record: CandidateRecord
    changed: tuple[str, ...]
    worker_outcome: WorkerOutcome
    step_results: tuple[StepResult, ...]
    evaluated: bool = False


# Usage is recorded by the agent layer and aggregated by observability. It lives
# at the package root because both layers need it: while it was owned by either
# one, that package and the other became mutually dependent.
_NUMERIC_FIELDS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)

@dataclasses.dataclass(frozen=True)
class UsageRecord:
    """One invocation's native usage; unavailable values stay ``None``."""

    source: str
    calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    lower_bounds: dict[str, int | float] = dataclasses.field(default_factory=dict)
    schema: str = dataclasses.field(default="usage_record/1", init=False)
    incomplete: bool = dataclasses.field(init=False)
    missing_fields: tuple[str, ...] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("UsageRecord.source must be nonempty.")
        for name in _NUMERIC_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"Usage field {name!r} must be nonnegative or None.")
            if name != "cost_usd" and not isinstance(value, int):
                raise TypeError(f"Usage field {name!r} must be an integer or None.")
        missing = tuple(name for name in _NUMERIC_FIELDS if getattr(self, name) is None)
        object.__setattr__(self, "missing_fields", missing)
        object.__setattr__(self, "incomplete", bool(missing))

    @classmethod
    def from_values(cls, *, source: str, **values: Any) -> UsageRecord:
        """Construct a record from provider-native values."""

        return cls(source=source, **values)

    @classmethod
    def unavailable(cls, source: str = "unavailable") -> UsageRecord:
        return cls(source=source)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def worker_exit_warning(outcome: Any) -> str | None:
    """Describe an abnormal worker exit without judging its workspace.

    Takes the outcome structurally rather than by type so that this root-level
    module never imports the agent layer, which already imports this one. Both
    the agent layer and observability need this projection, so the root is its
    only home that keeps the layer graph acyclic.
    """

    if outcome is None:
        return None
    if outcome.returncode == 0 and not outcome.timed_out and not outcome.cancelled:
        return None
    parts = [f"worker_exit_warning: returncode={outcome.returncode}"]
    if outcome.timed_out:
        parts.append("timed_out")
    if outcome.cancelled:
        parts.append("cancelled")
    if outcome.termination_reason:
        parts.append(f"termination={outcome.termination_reason}")
    return ", ".join(parts)
