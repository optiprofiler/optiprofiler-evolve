"""Explicit island-archive retention and parent-sampling strategies."""

from __future__ import annotations

import random
from collections.abc import Sequence

from .models import CandidateRecord, MetricBundle


class MetricSelectionError(ValueError):
    """A candidate cannot participate in the configured metric comparison."""


class ValidationLexicographicRetention:
    """The original validation-first bounded-island archive policy."""

    name = "validation_lexicographic"

    def retain(
        self,
        candidates: Sequence[CandidateRecord],
        capacity: int,
    ) -> tuple[CandidateRecord, ...]:
        if capacity < 1:
            raise ValueError("Archive capacity must be positive.")
        unique = {record.candidate_id: record for record in candidates}
        ordered = sorted(
            unique.values(),
            key=lambda record: (
                -record.validation_score,
                -record.public_score,
                -record.iteration,
                record.candidate_id,
            ),
        )
        return tuple(ordered[:capacity])


class TopBiasedValidationWeightedSampler:
    """Select the leader greedily or sample by nonnegative validation weight."""

    name = "top_biased_validation_weighted"

    def __init__(self, *, greedy_ratio: float = 0.7) -> None:
        if not 0 <= greedy_ratio <= 1:
            raise ValueError("greedy_ratio must be in [0, 1].")
        self.greedy_ratio = greedy_ratio

    def select(
        self,
        population: Sequence[CandidateRecord],
        rng: random.Random,
    ) -> CandidateRecord:
        if not population:
            raise ValueError("Cannot sample a parent from an empty island archive.")
        if len(population) == 1 or rng.random() < self.greedy_ratio:
            return population[0]
        weights = [max(record.validation_score, 1e-6) for record in population]
        return rng.choices(list(population), weights=weights, k=1)[0]


class MetricParetoRetention:
    """Retain nondominated metric fronts with deterministic scalar tie breaks."""

    name = "metric_pareto"

    def __init__(
        self,
        *,
        objectives: Sequence[str] = ("fitness",),
        epsilon: float = 0.0,
    ) -> None:
        self.objectives = tuple(objectives)
        if not self.objectives or len(self.objectives) != len(set(self.objectives)):
            raise ValueError("Pareto objectives must be a nonempty unique sequence.")
        if epsilon < 0:
            raise ValueError("Pareto epsilon cannot be negative.")
        self.epsilon = epsilon
        self.scalar_fallback = ValidationLexicographicRetention()

    def retain(
        self,
        candidates: Sequence[CandidateRecord],
        capacity: int,
    ) -> tuple[CandidateRecord, ...]:
        if capacity < 1:
            raise ValueError("Archive capacity must be positive.")
        if not candidates:
            return ()
        if len(self.objectives) == 1:
            return self.scalar_fallback.retain(candidates, capacity)
        ordered = list(self.scalar_fallback.retain(candidates, len(candidates)))
        bundles = self._comparable_bundles(ordered)
        retained: list[CandidateRecord] = []
        remaining = list(ordered)
        while remaining and len(retained) < capacity:
            front = [
                candidate
                for candidate in remaining
                if not any(
                    other.candidate_id != candidate.candidate_id
                    and self._dominates(
                        bundles[other.candidate_id], bundles[candidate.candidate_id]
                    )
                    for other in remaining
                )
            ]
            if not front:
                raise RuntimeError("Pareto sorting produced an empty front.")
            retained.extend(front[: capacity - len(retained)])
            front_ids = {record.candidate_id for record in front}
            remaining = [
                record for record in remaining if record.candidate_id not in front_ids
            ]
        return tuple(retained)

    def _comparable_bundles(
        self, candidates: Sequence[CandidateRecord]
    ) -> dict[str, MetricBundle]:
        bundles: dict[str, MetricBundle] = {}
        reference: MetricBundle | None = None
        for candidate in candidates:
            bundle = candidate.selection_metrics
            if bundle is None:
                raise MetricSelectionError(
                    f"Candidate {candidate.candidate_id!r} has no selection metric bundle."
                )
            try:
                if reference is not None:
                    reference.assert_comparable(bundle)
                for objective in self.objectives:
                    metric = bundle.metric(objective)
                    if not metric.valid or metric.value is None:
                        raise MetricSelectionError(
                            f"Candidate {candidate.candidate_id!r} has an invalid "
                            f"objective {objective!r}."
                        )
            except (KeyError, ValueError) as exc:
                if isinstance(exc, MetricSelectionError):
                    raise
                raise MetricSelectionError(str(exc)) from exc
            reference = reference or bundle
            bundles[candidate.candidate_id] = bundle
        return bundles

    def _dominates(self, left: MetricBundle, right: MetricBundle) -> bool:
        no_worse = True
        strictly_better = False
        for objective in self.objectives:
            left_metric = left.metric(objective)
            right_metric = right.metric(objective)
            assert left_metric.value is not None
            assert right_metric.value is not None
            sign = 1.0 if left_metric.direction == "max" else -1.0
            left_value = sign * float(left_metric.value)
            right_value = sign * float(right_metric.value)
            no_worse = no_worse and left_value >= right_value - self.epsilon
            strictly_better = strictly_better or left_value > right_value + self.epsilon
        return no_worse and strictly_better


__all__: list[str] = []
