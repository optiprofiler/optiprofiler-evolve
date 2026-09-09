from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_config import minimal_config
from test_engine import fake_agent_runner, fake_evaluator_factory
from worker_helpers import RunnerAdapter

from optiprofiler_evolve.config import AssessmentConfig, CriterionConfig, load_config
from optiprofiler_evolve.controller.assessment import PublicAssessment
from optiprofiler_evolve.controller.engine import EvolutionEngine
from optiprofiler_evolve.controller.registry import register
from optiprofiler_evolve.criteria.contracts import AssessmentRequest, Criterion, CriterionResult
from optiprofiler_evolve.criteria.integrity.execution import IntegrityReviewExecutor
from optiprofiler_evolve.criteria.runner import AssessmentRunner, ScoreAggregator
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash


class ConstantCriterion(Criterion):
    @classmethod
    def generation_guidance(cls, request):
        return "Optimize the configured constant test criterion."

    def assess(self, request):
        return CriterionResult("succeeded", score=request.options.get("score", 3.0), satisfied=True)


class AssessmentTests(unittest.TestCase):
    def test_source_mutation_is_rejected_even_for_zero_weight_advice(self):
        class MutatingCriterion(ConstantCriterion):
            def assess(self, request):
                (request.candidate / "solver.py").write_text("changed = True\n")
                return CriterionResult("succeeded", score=10, satisfied=True)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "solver.py").write_text("original = True\n")
            request = AssessmentRequest(candidate, tree_hash(candidate), "public", root / "out")
            report = AssessmentRunner({"mutating": MutatingCriterion()}).run(
                request, {"mutating": CriterionConfig(weight=0, mode="warn")}
            )
            self.assertFalse(report.accepted)
            self.assertIsNone(report.total)

    def test_review_outage_cache_is_scoped_to_exact_inputs(self):
        config = load_config(minimal_config())
        reviewer = Mock()
        reviewer.review.side_effect = RuntimeError("offline")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "solver.py").write_text("original = True\n")
            executor = IntegrityReviewExecutor(
                run_dir=root, interface=InterfaceSpec.parse("solver.py:solver"), editable=(".",),
                workers=config.workers, reviewer_config=config.reviewer_config,
                run_agent=Mock(), emit=Mock(),
            )
            def review():
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    executor.review(candidate_id="test", candidate=candidate, parent=candidate,
                                    changed=(), mutation_transcript=None, mutation_worker=None)

            with patch("optiprofiler_evolve.criteria.integrity.execution.build", return_value=reviewer):
                review()
                calls = reviewer.review.call_count
                review()
                self.assertEqual(reviewer.review.call_count, calls)
                (candidate / "solver.py").write_text("changed = True\n")
                review()
                self.assertEqual(reviewer.review.call_count, 2 * calls)

    def test_disabled_reviewer_does_not_require_unused_environment_variables(self):
        raw = minimal_config()
        raw["assessment"]["criteria"]["integrity"] = {
            "enabled": False, "options": {
                "worker": {"harness": "codex", "model": "${OPE_UNUSED_REVIEW_MODEL}"},
            },
        }
        config = load_config(raw)
        self.assertFalse(config.assessment.enabled("integrity"))

    def test_profile_weights_and_hidden_results_do_not_change_the_formal_score(self):
        register("criterion", "constant_a", ConstantCriterion, replace=True)
        register("criterion", "constant_b", ConstantCriterion, replace=True)
        raw = minimal_config()
        raw["assessment"] = {
            "criteria": {
                "constant_a": {"weight": 3, "options": {"score": 2}},
                "constant_b": {"weight": 1, "options": {"score": 10}},
            },
            "profiles": {
                "validation": {"constant_a": {"weight": 1}, "constant_b": {"weight": 3}},
                "hidden": {"constant_a": {"options": {"score": -100}}},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "solver.py"
            source.write_text("def solver(fun, x0):\n    return x0\n")
            engine = EvolutionEngine(
                initial=source, interface=InterfaceSpec.parse("solver.py:solver"), runtime="python",
                editable=(".",), config=load_config(raw), run_dir=root / "run",
                worker_adapter=RunnerAdapter(fake_agent_runner),
                evaluator_factory=Mock(side_effect=AssertionError("inactive backend")),
            )
            result = engine.run()
            self.assertEqual(result.assessment_score, 8)
            self.assertEqual(result.final_assessment_score, -72.5)
            public = json.loads((root / "run/controller/evaluations/seed/public/assessment.json").read_text())
            self.assertEqual(public["total"], 4)
            self.assertEqual(public["criteria"]["constant_a"]["weight"], 3)
            public_events = (root / "run/public_events.jsonl").read_text()
            self.assertNotIn("-72.5", public_events)
            self.assertNotIn('"assessment_score"', public_events)
            self.assertEqual(engine.champion[1].candidate_id, result.best_candidate_id)

    def test_unbounded_performance_scores_survive_the_complete_pipeline(self):
        class LargeScoreEvaluator:
            def evaluate(self, candidate, mode, output_dir):
                output_dir.mkdir(parents=True, exist_ok=True)
                score = 3.0 if "improved" in (candidate / "solver.py").read_text() else 2.0
                return EvaluationResult(mode, score, score, 2.0, 1, output_dir)

        raw = minimal_config()
        raw["assessment"]["criteria"] = {"performance": {"weight": 7}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "solver.py"
            source.write_text("def solver(fun, x0):\n    return x0\n")
            result = EvolutionEngine(
                initial=source, interface=InterfaceSpec.parse("solver.py:solver"), runtime="python",
                editable=(".",), config=load_config(raw), run_dir=root / "run",
                worker_adapter=RunnerAdapter(fake_agent_runner),
                evaluator_factory=lambda **kwargs: LargeScoreEvaluator(),
            ).run()
            self.assertEqual(result.assessment_score, 3)
            self.assertEqual(result.validation_score, 3)
            self.assertEqual(result.final_assessment_score, 3)
            self.assertIn("3.000000", (root / "run/FINAL_REPORT.md").read_text())

    def test_independent_weights_do_not_clip_or_rescale_scores(self):
        results = {name: CriterionResult("succeeded", score=score, satisfied=True)
                   for name, score in zip(("a", "b", "c"), (2.0, 3.0, 1.0))}
        specs = {name: CriterionConfig(weight=weight)
                 for name, weight in zip(results, (7, 2, 1))}
        total, accepted = ScoreAggregator.aggregate(results, specs)
        self.assertAlmostEqual(total, 2.1)
        self.assertTrue(accepted)
        results["a"] = CriterionResult("succeeded", score=-20, satisfied=True)
        self.assertAlmostEqual(ScoreAggregator.aggregate(results, specs)[0], -13.3)

    def test_missing_weighted_score_is_not_dropped_or_zero_filled(self):
        results = {"a": CriterionResult("failed", error="unavailable"),
                   "b": CriterionResult("succeeded", score=100, satisfied=True)}
        specs = {"a": CriterionConfig(mode="warn"), "b": CriterionConfig()}
        self.assertEqual(ScoreAggregator.aggregate(results, specs), (None, False))
        specs["a"] = CriterionConfig(mode="warn", weight=0)
        self.assertEqual(ScoreAggregator.aggregate(results, specs), (100, True))
        specs["a"] = CriterionConfig(mode="enforce", weight=0)
        self.assertEqual(ScoreAggregator.aggregate(results, specs), (100, False))

    def test_warn_constraint_still_contributes_its_score(self):
        results = {"a": CriterionResult("succeeded", score=2, satisfied=False)}
        self.assertEqual(ScoreAggregator.aggregate(results, {"a": CriterionConfig(mode="warn")}),
                         (2, True))
        self.assertEqual(ScoreAggregator.aggregate(results, {"a": CriterionConfig()}), (2, False))

    def test_disabled_or_zero_weight_formal_assessment_cannot_start(self):
        for criteria in ({}, {"x": CriterionConfig(enabled=False)},
                         {"x": CriterionConfig(weight=0)}):
            with self.subTest(criteria=criteria), self.assertRaisesRegex(ValueError, "positive weight"):
                AssessmentConfig(criteria=criteria).validate()
        config = AssessmentConfig(criteria={"x": CriterionConfig()},
                                  profiles={"validation": {"x": {"enabled": False}}})
        with self.assertRaises(ValueError):
            config.validate()

    def test_profile_options_weights_and_master_switch(self):
        config = AssessmentConfig(criteria={
            "a": CriterionConfig(weight=2, options={"x": 1, "y": 2}),
            "b": CriterionConfig(enabled=False),
        }, profiles={"smoke": {"a": {"weight": 7, "options": {"x": 9}},
                               "b": {"enabled": True}}})
        self.assertEqual(config.resolve("smoke")["a"].weight, 7)
        self.assertEqual(dict(config.resolve("smoke")["a"].options), {"x": 9, "y": 2})
        self.assertFalse(config.resolve("smoke")["b"].enabled)
        self.assertEqual(config.resolve("validation")["a"].weight, 2)

    def test_scores_and_weights_reject_nonfinite_values(self):
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CriterionResult("succeeded", score=value, satisfied=True)
                with self.assertRaises(ValueError):
                    CriterionConfig(weight=value)

    def test_runner_detects_snapshot_changes_and_never_invokes_disabled_criterion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "solver.py").write_text("value = 1\n")
            request = AssessmentRequest(candidate, tree_hash(candidate), "public", root / "out")
            disabled = Mock(spec=Criterion)
            disabled.version = "1"
            report = AssessmentRunner({"a": ConstantCriterion(), "b": disabled}).run(
                request, {"a": CriterionConfig(), "b": CriterionConfig(enabled=False)}
            )
            self.assertEqual(report.total, 3)
            disabled.assess.assert_not_called()
            (candidate / "solver.py").write_text("value = 2\n")
            report = AssessmentRunner({"a": ConstantCriterion()}).run(request, {"a": CriterionConfig()})
            self.assertFalse(report.accepted)
            self.assertIsNone(report.total)

    def test_public_adapter_rejects_private_profiles_before_callback(self):
        callback = Mock()
        public = PublicAssessment(callback)
        for profile in ("validation", "hidden"):
            with self.assertRaises(ValueError):
                public.evaluate(Path("candidate"), profile, Path("out"))
        callback.assert_not_called()

    def test_each_standard_can_run_alone_without_inactive_backends(self):
        for name in ("performance", "complexity", "integrity", "constant_test"):
            with self.subTest(criterion=name), tempfile.TemporaryDirectory() as directory:
                register("criterion", "constant_test", ConstantCriterion, replace=True)
                root = Path(directory)
                source = root / "solver.py"
                source.write_text("def solver(fun, x0):\n    return x0\n")
                raw = minimal_config()
                selected = raw["assessment"]["criteria"].get(name, {})
                selected["enabled"] = True
                raw["assessment"]["criteria"] = {name: selected}
                if name != "performance":
                    raw["data"] = {"library": "must_not_import"}
                    raw["execution"] = {}
                backend = fake_evaluator_factory if name == "performance" else Mock(
                    side_effect=AssertionError("disabled evaluator was initialized")
                )
                engine = EvolutionEngine(
                    initial=source, interface=InterfaceSpec.parse("solver.py:solver"),
                    runtime="python", editable=(".",), config=load_config(raw),
                    run_dir=root / "run", worker_adapter=RunnerAdapter(fake_agent_runner),
                    evaluator_factory=backend,
                )
                result = engine.run()
                self.assertIsNotNone(result.assessment_score)
                if name != "performance":
                    backend.assert_not_called()
                    self.assertIsNone(result.public_score)
                    self.assertIsNone(result.validation_score)
                    self.assertIsNone(result.final_score)
                if name == "constant_test":
                    self.assertEqual(result.assessment_score, 3)


if __name__ == "__main__":
    unittest.main()
