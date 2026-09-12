from __future__ import annotations

import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from optiprofiler_evolve.config import AssessmentConfig
from optiprofiler_evolve.controller.selection import (
    MetricParetoRetention,
    MetricSelectionError,
    TopBiasedValidationWeightedSampler,
    ValidationLexicographicRetention,
)
from optiprofiler_evolve.criteria.contracts import CriterionResult
from optiprofiler_evolve.criteria.runner import ScoreAggregator
from optiprofiler_evolve.models import CandidateRecord, EvaluationResult, MetricBundle, MetricValue


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
        assessment_score=validation,
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
    def test_default_integrity_preserves_sampling_weights_and_explicit_weight_is_honored(self):
        for config in (AssessmentConfig(), AssessmentConfig(criteria={
            "performance": {}, "integrity": {},
        })):
            specs = config.resolve("validation")
            population = []
            for name, score in (("high", 0.9), ("low", 0.1)):
                results = {
                    "performance": CriterionResult("succeeded", score=score, satisfied=True),
                    "integrity": CriterionResult("succeeded", score=1.0, satisfied=True),
                }
                total, accepted = ScoreAggregator.aggregate(results, specs)
                self.assertTrue(accepted)
                self.assertEqual(total, score)
                population.append(replace(candidate(name, score, score), assessment_score=total))
            sampler = TopBiasedValidationWeightedSampler(greedy_ratio=0)
            for seed in range(30):
                rng = random.Random(seed)
                rng.random()
                expected = rng.choices(population, weights=[0.9, 0.1])[0]
                self.assertEqual(sampler.select(population, random.Random(seed)), expected)
            results["integrity"] = CriterionResult("succeeded", score=0, satisfied=False)
            self.assertFalse(ScoreAggregator.aggregate(results, specs)[1])
        explicit = AssessmentConfig(criteria={"integrity": {"weight": 2}})
        self.assertEqual(explicit.resolve("validation")["integrity"].weight, 2)

    def test_public_score_breaks_formal_ties_before_iteration(self):
        older_better = candidate("older", 0.7, 0.9, iteration=1)
        newer_worse = candidate("newer", 0.7, 0.1, iteration=2)
        self.assertEqual(ValidationLexicographicRetention().retain(
            (newer_worse, older_better), 1,
        ), (older_better,))

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

    def test_single_objective_pareto_ranks_by_the_objective_metric(self) -> None:
        # `metric_pareto` with one objective and epsilon 0 does NOT delegate to
        # `validation_lexicographic`; it orders by the objective's metric value.
        # The fixture makes those two orders disagree on purpose, because when
        # `assessment_score` equals the bundle value (as the old fixture had it)
        # both orders coincide and the assertion would hold by construction.
        records = [
            candidate("a", 0.5, 0.8, metrics=MetricBundle.scalar(0.9, invariants_hash="fixed")),
            candidate("b", 0.9, 0.4, metrics=MetricBundle.scalar(0.8, invariants_hash="fixed")),
        ]
        scalar = ValidationLexicographicRetention().retain(records, 1)
        pareto = MetricParetoRetention(objectives=("fitness",)).retain(records, 1)
        self.assertEqual([item.candidate_id for item in scalar], ["b"])
        self.assertEqual([item.candidate_id for item in pareto], ["a"])

    def test_single_objective_pareto_agrees_with_scalar_when_orders_coincide(self) -> None:
        # The narrow case the previous test name claimed: when the assessed score
        # and the objective value rank the same way, both policies agree.
        records = [
            candidate("a", 0.5, 0.8, metrics=MetricBundle.scalar(0.5, invariants_hash="fixed")),
            candidate("b", 0.7, 0.4, metrics=MetricBundle.scalar(0.7, invariants_hash="fixed")),
        ]
        scalar = ValidationLexicographicRetention().retain(records, 1)
        pareto = MetricParetoRetention(objectives=("fitness",)).retain(records, 1)
        self.assertEqual([item.candidate_id for item in pareto],
                         [item.candidate_id for item in scalar])

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
