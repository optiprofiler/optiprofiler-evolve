from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.models import (
    CandidateRecord,
    EvaluationResult,
    MetricBundle,
    MetricValue,
)
from optiprofiler_evolve.selection import (
    MetricParetoRetention,
    MetricSelectionError,
    TopBiasedValidationWeightedSampler,
    ValidationLexicographicRetention,
)


def candidate(
    candidate_id: str,
    validation: float,
    public: float,
    *,
    iteration: int = 1,
    metrics: MetricBundle | None = None,
) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=candidate_id,
        island=0,
        iteration=iteration,
        attempt_index=0,
        parent_id="seed",
        path=Path("/") / candidate_id,
        tree_hash=candidate_id,
        public_score=public,
        validation_score=validation,
        selection_metrics=metrics,
    )


def two_metric_bundle(
    speed: float,
    robustness: float,
    *,
    invariants_hash: str = "fixed-experiment",
) -> MetricBundle:
    return MetricBundle(
        primary="speed",
        invariants_hash=invariants_hash,
        metrics=(
            MetricValue("speed", speed, "max", "profile_integral"),
            MetricValue("robustness", robustness, "max", "merit_decrease"),
        ),
    )


class SelectionTests(unittest.TestCase):
    def test_metric_bundle_validates_primary_and_comparability(self) -> None:
        bundle = two_metric_bundle(0.7, 0.6)
        self.assertEqual(bundle.metric("speed").value, 0.7)
        self.assertEqual(len(bundle.metric_set_id), 64)
        bundle.assert_comparable(two_metric_bundle(0.5, 0.8))
        with self.assertRaisesRegex(ValueError, "incompatible"):
            bundle.assert_comparable(
                two_metric_bundle(0.5, 0.8, invariants_hash="different-experiment")
            )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must equal"):
                EvaluationResult(
                    "validation",
                    0.5,
                    0.5,
                    0.5,
                    1,
                    Path(directory),
                    metric_bundle=bundle,
                )
            result = EvaluationResult(
                "validation",
                0.7,
                0.7,
                0.5,
                1,
                Path(directory),
                metric_bundle=bundle,
            )
            self.assertNotIn("metric_bundle", result.as_dict())
            self.assertIn(
                "metric_bundle",
                result.as_dict(include_metric_bundle=True),
            )

    def test_default_retention_matches_original_lexicographic_order(self) -> None:
        records = [
            candidate("c", 0.7, 0.4, iteration=1),
            candidate("a", 0.7, 0.5, iteration=1),
            candidate("b", 0.7, 0.5, iteration=2),
            candidate("d", 0.6, 0.9, iteration=4),
            candidate("a", 0.8, 0.1, iteration=5),
        ]
        original_unique = {record.candidate_id: record for record in records}
        expected = sorted(
            original_unique.values(),
            key=lambda record: (
                -record.validation_score,
                -record.public_score,
                -record.iteration,
                record.candidate_id,
            ),
        )[:3]
        actual = ValidationLexicographicRetention().retain(records, 3)
        self.assertEqual(
            [record.candidate_id for record in actual],
            [record.candidate_id for record in expected],
        )

    def test_default_sampler_matches_original_seeded_sequence(self) -> None:
        population = ValidationLexicographicRetention().retain(
            [
                candidate("a", 0.8, 0.5),
                candidate("b", 0.6, 0.7),
                candidate("c", 0.4, 0.9),
            ],
            3,
        )
        sampler = TopBiasedValidationWeightedSampler(greedy_ratio=0.7)
        for seed in range(30):
            old_rng = random.Random(seed)
            if len(population) == 1 or old_rng.random() < 0.7:
                expected = population[0]
            else:
                expected = old_rng.choices(
                    list(population),
                    weights=[max(record.validation_score, 1e-6) for record in population],
                    k=1,
                )[0]
            actual = sampler.select(population, random.Random(seed))
            self.assertEqual(actual.candidate_id, expected.candidate_id)

    def test_single_objective_pareto_delegates_to_scalar_retention(self) -> None:
        records = [candidate("a", 0.5, 0.8), candidate("b", 0.7, 0.4)]
        scalar = ValidationLexicographicRetention().retain(records, 1)
        pareto = MetricParetoRetention(objectives=("fitness",)).retain(records, 1)
        self.assertEqual(pareto, scalar)

    def test_multiobjective_pareto_keeps_nondominated_front(self) -> None:
        records = [
            candidate("fast", 0.7, 0.7, metrics=two_metric_bundle(0.9, 0.4)),
            candidate("robust", 0.6, 0.6, metrics=two_metric_bundle(0.4, 0.9)),
            candidate("dominated", 0.9, 0.9, metrics=two_metric_bundle(0.3, 0.3)),
        ]
        retained = MetricParetoRetention(
            objectives=("speed", "robustness")
        ).retain(records, 2)
        self.assertEqual({record.candidate_id for record in retained}, {"fast", "robust"})

    def test_multiobjective_pareto_fails_closed_on_incompatible_bundle(self) -> None:
        records = [
            candidate("a", 0.7, 0.7, metrics=two_metric_bundle(0.9, 0.4)),
            candidate(
                "b",
                0.6,
                0.6,
                metrics=two_metric_bundle(0.4, 0.9, invariants_hash="other"),
            ),
        ]
        with self.assertRaises(MetricSelectionError):
            MetricParetoRetention(objectives=("speed", "robustness")).retain(records, 2)


if __name__ == "__main__":
    unittest.main()
