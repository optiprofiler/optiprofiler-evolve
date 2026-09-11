from __future__ import annotations

import json
import math
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

from test_config import minimal_config
from test_extensibility import evaluator_factory
from worker_helpers import RunnerAdapter

from optiprofiler_evolve.agents.broker import EvaluationBroker
from optiprofiler_evolve.agents.contracts import AgentJob, WorkerOutcome
from optiprofiler_evolve.agents.feedback import project_feedback
from optiprofiler_evolve.application.run import EvolutionRun
from optiprofiler_evolve.config import ComplexityConfig, SandboxConfig, load_config
from optiprofiler_evolve.controller.assessment import PublicAssessment
from optiprofiler_evolve.controller.contracts import VariantRequest
from optiprofiler_evolve.criteria.complexity.analyzer import (
    ComplexityChecker,
    ComplexityMetrics,
    analyze_source,
)
from optiprofiler_evolve.criteria.complexity.criterion import ComplexityCriterion, ComplexityReport
from optiprofiler_evolve.criteria.contracts import AssessmentRequest
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.observability.events import EventWriter, rebuild_run_state
from optiprofiler_evolve.observability.projections import project_public_events
from optiprofiler_evolve.observability.trace_ledger import TraceLedger
from optiprofiler_evolve.observability.views.public import render_public_report, render_status
from optiprofiler_evolve.phases.attempts.contracts import StepResult
from optiprofiler_evolve.phases.prepare import PreparePhase
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash

SEED = "def solver(fun, x0):\n    return x0\n"
LARGE = "def solver(fun, x0):\n    if len(x0):\n        x0 = x0.copy()\n    return x0\n"


class RecordingEvaluator:
    def __init__(self):
        self.calls = []

    def evaluate(self, candidate, mode, output_dir):
        self.calls.append((candidate, mode, (candidate / "solver.py").read_text()))
        output_dir.mkdir(parents=True, exist_ok=True)
        return EvaluationResult(mode, 0.5, 0.5, 0.5, 1, output_dir)


class ComplexityTests(unittest.TestCase):
    def test_each_profile_persists_its_own_policy_and_consistent_evidence(self):
        checker = self.checker()
        criterion = ComplexityCriterion(checker, mode="enforce")
        result = criterion.assess(AssessmentRequest(
            self.candidate, "unused", "validation", self.root / "validation",
            options={"max_ratio": 1.1},
        ))
        self.assertFalse(result.satisfied)
        self.assertEqual(result.metrics["mode"], "enforce")
        self.assertEqual(result.metrics["max_ratio"], 1.1)
        self.assertTrue(result.metrics["blocked"])
        public = ComplexityCriterion(checker, mode="warn").assess(AssessmentRequest(
            self.candidate, tree_hash(self.candidate), "public", self.root / "public",
            options={"max_ratio": 3.0},
        ))
        self.assertTrue(public.satisfied)
        for profile, expected in (("public", public), ("validation", result)):
            saved = json.loads((self.root / profile / expected.evidence[0]).read_text())
            for key in ("mode", "max_ratio", "status", "blocked", "score"):
                self.assertEqual(saved[key], expected.metrics[key])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.source("seed", SEED)
        self.candidate = self.source("candidate", LARGE)

    def source(self, name, code):
        root = self.root / name
        root.mkdir(parents=True)
        (root / "solver.py").write_text(code)
        return root

    def checker(self):
        return ComplexityChecker(
            self.seed, SandboxConfig(), self.root / "analysis"
        )

    def report(self, checker, root, mode="warn", ratio=1.2):
        output = self.root / "reports" / uuid.uuid4().hex
        result = ComplexityCriterion(checker, mode=mode).assess(AssessmentRequest(
            root, tree_hash(root), "public", output, options={"max_ratio": ratio},
        ))
        saved = json.loads((output / result.evidence[0]).read_text())
        saved["baseline"] = ComplexityMetrics(**saved["baseline"])
        if saved["candidate"] is not None:
            saved["candidate"] = ComplexityMetrics(**saved["candidate"])
        return ComplexityReport(**{f.name: saved[f.name] for f in fields(ComplexityReport)})

    def test_config_defaults_and_invalid_values(self):
        self.assertIsNone(load_config(minimal_config()).complexity_config())
        raw = minimal_config()
        raw["assessment"]["criteria"]["complexity"] = {"enabled": True, "mode": "warn"}
        self.assertEqual(load_config(raw).complexity_config(), ComplexityConfig())
        raw["assessment"]["criteria"]["complexity"]["enabled"] = False
        self.assertIsNone(load_config(raw).complexity_config())
        for ratio in (0, -1, math.inf, math.nan, True, "1.2"):
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                ComplexityConfig(max_ratio=ratio).validate()
        with self.assertRaises(ValueError):
            ComplexityConfig(mode="optional").validate()
        ComplexityConfig(max_ratio=0.8).validate()

    def test_comments_docstrings_and_semicolon_formatting(self):
        a = self.source(
            "a",
            '"""module docs"""\ndef f():\n    """function\n    docs"""\n'
            "    a = 1; b = 2\n    return a + b\n",
        )
        b = self.source(
            "b", "def f():\n    # documentation\n    a = 1\n\n    b = 2\n    return a + b\n"
        )
        ma, mb = analyze_source(a), analyze_source(b)
        self.assertEqual(
            (ma.decision_complexity, ma.logical_lines), (mb.decision_complexity, mb.logical_lines)
        )

    def test_all_scopes_and_new_files_count_without_class_double_counting(self):
        code = (
            "if True:\n    pass\nclass A:\n    if True:\n        pass\n"
            "    def f(self, x):\n        def g():\n            if x:\n"
            "                return 1\n        if x:\n            return g()\n"
        )
        root = self.source("scopes", code)
        self.assertEqual(analyze_source(root).decision_complexity, 5)
        (root / "tests").mkdir()
        (root / "tests" / "helper.py").write_text("if True:\n    pass\n")
        self.assertEqual(analyze_source(root).decision_complexity, 6)
        (root / ".optiprofiler_evolve").mkdir()
        (root / ".optiprofiler_evolve" / "ignored.py").write_text(LARGE)
        self.assertEqual(analyze_source(root).decision_complexity, 6)

    def test_extracting_helpers_cannot_dilute_branch_total(self):
        a = self.source("inline", "def f(x):\n    if x:\n        return 1\n    return 0\n")
        b = self.source("extracted", "def f(x):\n    return g(x)\n")
        (b / "helpers.py").write_text("def g(x):\n    if x:\n        return 1\n    return 0\n")
        self.assertEqual(
            analyze_source(a).decision_complexity, analyze_source(b).decision_complexity
        )

    def test_boundaries_fixed_baseline_cache_and_source_invalidation(self):
        checker = self.checker()
        self.assertEqual(self.report(checker, self.seed).summary()["score"], 0.5)
        self.assertEqual(self.report(checker, self.seed).summary()["complexity_percent"], 100)
        self.assertEqual(self.report(checker, self.candidate, "enforce", 2).status, "within_limit")
        with patch.object(checker, "_analyze", wraps=checker._analyze) as analyzer:
            self.report(checker, self.candidate)
            analyzer.assert_not_called()
            (self.candidate / "solver.py").write_text(LARGE + "if True:\n    pass\n")
            report = self.report(checker, self.candidate, "enforce", 2)
            self.assertTrue(report.blocked)
            self.assertEqual(report.baseline_hash, checker.baseline_hash)
            self.assertEqual(analyzer.call_count, 1)

    def test_empty_size_baseline_has_finite_ratio(self):
        (self.seed / "solver.py").write_text("")
        report = self.report(self.checker(), self.candidate)
        self.assertTrue(math.isfinite(report.summary()["complexity_ratio"]))

    def test_analysis_of_distinct_candidates_runs_concurrently(self):
        checker = self.checker()
        other = self.source("other", LARGE + "extra = 1\n")
        barrier = threading.Barrier(2, timeout=5)

        def analyze(root):
            barrier.wait()
            return analyze_source(root)

        with patch.object(checker, "_analyze", side_effect=analyze):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reports = list(pool.map(checker.measure, (self.candidate, other)))
        self.assertEqual(reports, [analyze_source(self.candidate), analyze_source(other)])

    def test_concurrent_identical_sources_publish_a_valid_cached_report(self):
        checker = self.checker()
        other = self.source("other", LARGE)
        barrier = threading.Barrier(2, timeout=5)

        def analyze(root):
            barrier.wait()
            return analyze_source(root)

        with patch.object(checker, "_analyze", side_effect=analyze):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reports = list(pool.map(checker.measure, (self.candidate, other)))
        self.assertEqual(reports[0], reports[1])
        with patch.object(checker, "_analyze") as analyzer:
            self.assertEqual(checker.measure(other), reports[0])
            analyzer.assert_not_called()
        for path in (self.root / "analysis").glob("*.json"):
            json.loads(path.read_text())

    def test_source_changed_during_analysis_is_not_cached(self):
        checker = self.checker()

        def analyze(root):
            metrics = analyze_source(root)
            (root / "solver.py").write_text(SEED)
            return metrics

        with patch.object(checker, "_analyze", side_effect=analyze):
            self.assertEqual(self.report(checker, self.candidate).status, "unavailable")
        (self.candidate / "solver.py").write_text(LARGE)
        with patch.object(checker, "_analyze", wraps=checker._analyze) as analyzer:
            report = self.report(checker, self.candidate)
            analyzer.assert_called_once()
        self.assertEqual(report.score, 1 / (1 + max(report.ratios)))
        self.assertEqual(report.summary()["complexity_percent"], 100 * max(report.ratios))

    def test_analysis_programming_errors_propagate(self):
        checker = self.checker()
        with patch.object(checker, "_analyze", side_effect=AttributeError("bug")):
            with self.assertRaisesRegex(AttributeError, "bug"):
                self.report(checker, self.candidate)

    def test_each_metric_can_independently_exceed_the_limit(self):
        checker = self.checker()
        (self.candidate / "solver.py").write_text(SEED + "extra = 1\n")
        size = self.report(checker, self.candidate, "enforce")
        self.assertEqual(size.ratios, (1, 1.5))
        self.assertTrue(size.blocked)
        (self.candidate / "solver.py").write_text(
            "def solver(fun, x0):\n    return x0 if x0 else None\n"
        )
        branches = self.report(checker, self.candidate, "enforce")
        self.assertEqual(branches.ratios, (2, 1))
        self.assertTrue(branches.blocked)

    def test_invalid_source_has_no_numeric_score_and_baseline_failure_aborts(self):
        checker = self.checker()
        (self.candidate / "helper.py").write_text("def invalid(")
        report = self.report(checker, self.candidate)
        self.assertEqual(report.status, "unavailable")
        self.assertIsNone(report.summary()["score"])
        (self.seed / "solver.py").write_text("def invalid(")
        with self.assertRaisesRegex(ValueError, "analysis failed"):
            self.checker()

    def test_unwired_complexity_event_never_enters_public_projection(self):
        report = self.report(self.checker(), self.candidate)
        private = self.root / "events.jsonl"
        writer = EventWriter(private)
        writer.emit(
            "complexity_checked",
            "succeeded",
            data={
                **report.summary(),
                "baseline_source": "SECRET_SEED",
                "path": "/private/path",
                "validation_score": 0.91234,
                "hotspots": [{"name": "SECRET_FUNCTION"}],
            },
        )
        writer.close()
        public = self.root / "public_events.jsonl"
        project_public_events(private, public)
        state = rebuild_run_state(public)
        self.assertNotIn("complexity", state["run"])
        page = self.root / "public.html"
        render_status(public, page)
        render_public_report(state, self.root / "PUBLIC_REPORT.md")
        for path in (public, page, self.root / "PUBLIC_REPORT.md"):
            text = path.read_text()
            self.assertNotIn("complexity_checked", text)
            self.assertNotIn("SECRET", text)
            self.assertNotIn("/private/path", text)
            self.assertNotIn("0.91234", text)
            self.assertNotIn("Latest source complexity", text)

    def test_analysis_failures_and_timeouts_follow_mode(self):
        for mode in ("warn", "enforce"):
            checker = self.checker()
            with patch.object(checker, "_analyze", side_effect=ValueError("unavailable")):
                report = self.report(checker, self.candidate, mode)
            self.assertEqual(report.status, "unavailable")
            self.assertEqual(report.blocked, mode == "enforce")
            self.assertIsNone(report.summary()["score"])
        with patch(
            "optiprofiler_evolve.criteria.complexity.analyzer.subprocess.run",
            side_effect=subprocess.TimeoutExpired("analyze", 30),
        ), self.assertRaisesRegex(ValueError, "timed out"):
            self.checker()

    def test_analysis_never_executes_source(self):
        marker = self.root / "executed"
        (self.candidate / "solver.py").write_text(
            f"open({str(marker)!r}, 'w').write('bad')\n" + LARGE
        )
        self.report(self.checker(), self.candidate)
        self.assertFalse(marker.exists())

    def test_all_feedback_modes_include_safe_complexity_details(self):
        report = self.report(self.checker(), self.candidate)
        source = self.root / "evidence"
        source.mkdir()
        result = EvaluationResult("public", 0.5, 0.5, 0.5, 1, source, assessment={
            "profile": "public", "results": {"complexity": {
                "status": "succeeded", "score": report.score, "satisfied": False,
                "metrics": report.worker_dict(),
            }},
        })
        for mode, bad_receipt in (("score_only", False), ("full_artifacts", False),
                                  ("full_artifacts", True)):
            if bad_receipt:
                (source / "eval_report.json").write_text("invalid receipt")
            destination = self.root / f"{mode}-{bad_receipt}"
            warning = project_feedback(
                source,
                destination,
                result,
                mode=mode,
            )
            self.assertEqual(warning is not None, bad_receipt)
            payload = json.loads((destination / "complexity.json").read_text())
            self.assertNotIn("hotspots", payload["baseline"])
            self.assertNotIn("baseline_hash", payload)
            feedback = (destination / "feedback.md").read_text()
            self.assertIn("Simplify", feedback)
            self.assertEqual(feedback.count("Relative complexity:"), 1)

    def test_unavailable_complexity_withholds_private_error(self):
        report = replace(self.report(self.checker(), self.candidate), candidate=None,
                         error="private backend path and credentials")
        source = self.root / "evidence"
        source.mkdir()
        result = EvaluationResult("public", None, None, None, 0, source, success=False,
                                  assessment={"profile": "public", "results": {"complexity": {
                                      "status": "failed", "score": None, "satisfied": None,
                                      "metrics": report.worker_dict(),
                                  }}})
        destination = self.root / "feedback"
        project_feedback(source, destination, result, mode="score_only")
        self.assertIn("unavailable", (destination / "feedback.md").read_text())
        self.assertNotIn(report.error, (destination / "complexity.json").read_text())

    def test_broker_enforcement_quota_and_snapshot(self):
        for mode in ("warn", "enforce"):
            evaluator = RecordingEvaluator()
            engine = self.engine(mode, run_name=f"broker-{mode}",
                                 evaluator_factory=lambda **kwargs: evaluator)
            engine._initialize_run_directory()
            engine.coordinator.run_phase(PreparePhase(), {})
            evaluator.calls.clear()
            original_assess = engine.state.evaluator.complexity.measure

            def assess(snapshot):
                report = original_assess(snapshot)
                (self.candidate / "solver.py").write_text(SEED)
                return report

            engine.state.evaluator.complexity.measure = assess

            (self.candidate / "solver.py").write_text(LARGE)
            broker = EvaluationBroker(
                workspace=self.candidate,
                control_dir=self.root / mode,
                evaluator=PublicAssessment(engine.state.evaluator.evaluate),
                max_smoke_calls=1,
                max_public_calls=1,
            )
            for name in ("requests", "responses"):
                (broker.exchange / name).mkdir(parents=True)
            for call in range(2):
                request_id = uuid.uuid4().hex
                request = broker.exchange / "requests" / f"{request_id}.json"
                request.write_text(
                    json.dumps({"id": request_id, "token": broker.token, "mode": "public"})
                )
                broker._process(request)
                payload = json.loads((broker.exchange / "responses" / request.name).read_text())
                if call == 0:
                    self.assertEqual(payload["status"], 200)
                    self.assertFalse(payload["payload"]["assessment"]["results"]["complexity"]["satisfied"])
                    details = json.loads(
                        (broker.artifacts / "evaluations/public/001/complexity.json").read_text()
                    )
                    self.assertGreater(details["complexity_ratio"], 1.2)
                    self.assertTrue(details["candidate"]["hotspots"])
                    self.assertNotIn("baseline_hash", details)
                    self.assertNotIn("hotspots", details["baseline"])
                    self.assertIn("Logical lines", payload["payload"]["_feedback"])
                    if mode == "enforce":
                        self.assertIsNone(payload["payload"]["score"])
                else:
                    self.assertEqual(payload["payload"]["error"], "evaluation quota exhausted")
            self.assertEqual(len(evaluator.calls), int(mode == "warn"))
            if evaluator.calls:
                self.assertEqual(evaluator.calls[0][2], LARGE)

    def engine(self, mode, ratio=1.2, run_name="run", *,
               evaluator_factory=evaluator_factory, **criterion_options):
        raw = minimal_config()
        raw["assessment"]["criteria"]["complexity"] = {"enabled": True, "mode": mode, "options": {"max_ratio": ratio}}
        raw["assessment"]["criteria"]["complexity"].update(criterion_options)
        raw["evolution"].update(islands=1, attempts_per_island=1)

        def mutate(**kwargs):
            (kwargs["workspace"] / "solver.py").write_text(LARGE)
            kwargs["transcript"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["transcript"].write_text("synthetic mutation")
            return WorkerOutcome(0, kwargs["transcript"])

        return EvolutionRun(
            initial=self.seed,
            interface=InterfaceSpec.parse("solver.py:solver"),
            runtime="python",
            editable=(".",),
            config=load_config(raw),
            run_dir=self.root / run_name,
            worker_adapter=RunnerAdapter(mutate),
            evaluator_factory=evaluator_factory,
        )

    def test_enforced_candidate_never_reaches_performance_or_validation(self):
        evaluator = RecordingEvaluator()
        engine = self.engine("enforce", evaluator_factory=lambda **kwargs: evaluator)
        engine.run()
        self.assertFalse(engine.state.candidates["it001-i00-a00"].valid)
        self.assertTrue(all(code == SEED for _, _, code in evaluator.calls))

    def test_worker_receives_configured_complexity_prompt(self):
        for mode in (None, "warn", "enforce"):
            with self.subTest(mode=mode):
                engine = self.engine(mode or "warn", 1.75, run_name=f"worker-{mode}",
                                     enabled=mode is not None)
                prompts = []
                mutate = engine.worker_adapter.runner

                def capture(**kwargs):
                    prompts.append(kwargs["prompt"])
                    return mutate(**kwargs)

                engine.worker_adapter = RunnerAdapter(capture)
                engine.run()
                self.assertEqual(len(prompts), 1)
                prompt = prompts[0]
                if mode is None:
                    self.assertNotIn("Complexity budget", prompt)
                    self.assertNotIn("Current parent:", prompt)
                    continue
                self.assertIn(
                    f"Complexity budget ({mode}): branches and logical lines may each be at "
                    "most 1.75 times their initial values.", prompt
                )
                self.assertNotIn("Current parent:", prompt)
                self.assertIn("All candidate Python files count", prompt)
                self.assertIn("rather than compress formatting or remove documentation", prompt)
                self.assertEqual(prompt.count("Complexity budget ("), 1)
                self.assertNotIn(engine.state.evaluator.complexity.baseline_hash, prompt)
                self.assertNotIn(str(self.seed), prompt)

    def test_research_role_prompt_keeps_no_criterion_guidance(self):
        for mode in (None, "warn", "enforce"):
            with self.subTest(mode=mode):
                engine = self.engine(mode or "warn", 1.75, run_name=f"research-{mode}",
                                     enabled=mode is not None)
                prompts = []

                def capture(**kwargs):
                    prompts.append(kwargs["prompt"])
                    kwargs["transcript"].parent.mkdir(parents=True, exist_ok=True)
                    kwargs["transcript"].write_text("synthetic research")
                    return WorkerOutcome(0, kwargs["transcript"])

                engine.worker_adapter = RunnerAdapter(capture)
                engine._initialize_run_directory()
                engine.trace_ledger = TraceLedger(
                    engine.run_dir, run_id="prompt-test", config_hash="test",
                    adapter_name=engine.worker_adapter.name,
                )
                engine.coordinator.run_phase(PreparePhase(), {})
                job = AgentJob(
                    role="strategy-analyst", job_id="prompt-test", prompt="Analyze strategies."
                )
                engine.controller_services.run_trusted_agent(job)
                self.assertEqual(job.prompt, "Analyze strategies.")
                self.assertEqual(len(prompts), 1)
                # Research roles are not solver proposal/edit jobs, so the worker
                # task card and the role prompt stay exactly as authored.
                self.assertEqual(prompts[0], job.prompt)
                self.assertNotIn("Complexity budget", prompts[0])
                self.assertNotIn("Assessment criteria", prompts[0])

    def test_warn_candidate_is_admitted_and_reported(self):
        engine = self.engine("warn")
        engine.run()
        self.assertTrue(engine.state.candidates["it001-i00-a00"].valid)
        self.assertTrue(list((engine.run_dir / "controller/final_evaluations").glob("*/assessment.json")))
        self.assertIn("Quality assessment", (engine.run_dir / "FINAL_REPORT.md").read_text())

    def test_strict_reduction_without_eligible_candidate_skips_hidden(self):
        evaluator = RecordingEvaluator()
        engine = self.engine("enforce", 0.8, evaluator_factory=lambda **kwargs: evaluator)
        with self.assertRaisesRegex(RuntimeError, "Initial solver evaluation failed"):
            engine.run()
        self.assertNotIn("hidden", [mode for _, mode, _ in evaluator.calls])

    def test_complexity_weight_can_favor_simplification_over_performance(self):
        (self.seed / "solver.py").write_text(LARGE)

        def simplify(**kwargs):
            (kwargs["workspace"] / "solver.py").write_text(SEED)
            kwargs["transcript"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["transcript"].write_text("simplified")
            return WorkerOutcome(0, kwargs["transcript"])

        class PreferSeed(RecordingEvaluator):
            def evaluate(self, candidate, mode, output_dir):
                result = super().evaluate(candidate, mode, output_dir)
                score = 0.9 if (candidate / "solver.py").read_text() == LARGE else 0.1
                return EvaluationResult(mode, score, score, 0.5, 1, result.output_dir)

        engine = self.engine("enforce", 1.0, weight=10,
                             evaluator_factory=lambda **kwargs: PreferSeed())
        engine.worker_adapter = RunnerAdapter(simplify)
        result = engine.run()
        self.assertEqual(result.best_candidate_id, "it001-i00-a00")
        self.assertEqual(result.public_score, 0.1)

    def test_post_score_edits_cannot_bypass_final_gate(self):
        engine = self.engine("enforce", 2)

        class ExpandAfterScore:
            name = "expand_after_score"

            def run(self, context):
                (context.workspace / "solver.py").write_text(LARGE + "if True:\n    pass\n")
                return StepResult()

        engine.attempt_runner.environment = replace(
            engine.environment,
            attempt_steps=(*engine.environment.attempt_steps, ExpandAfterScore()),
        )
        engine.run()
        self.assertFalse(engine.state.candidates["it001-i00-a00"].valid)

    def test_research_variant_uses_same_limit(self):
        engine = self.engine("enforce")
        engine._initialize_run_directory()
        engine.coordinator.run_phase(PreparePhase(), {})
        variant = self.source("run/research/proposed", LARGE)
        handle = engine.controller_services.materialize_variant(
                VariantRequest("large", engine.run_dir / "candidates/seed", tree=variant)
            )
        result = engine.controller_services.evaluate_public_variant(handle, "check")
        self.assertFalse(result.success)
        self.assertIn("complexity_limit_exceeded", result.error)
