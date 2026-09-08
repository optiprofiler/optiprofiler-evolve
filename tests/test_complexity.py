from __future__ import annotations

import json
import math
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.broker import EvaluationBroker
from optiprofiler_evolve.complexity import (
    ComplexityChecker,
    ComplexityConfig,
    ComplexityLimitExceeded,
    analyze_source,
)
from optiprofiler_evolve.config import SandboxConfig, load_config
from optiprofiler_evolve.engine import EvolutionEngine
from optiprofiler_evolve.feedback import project_feedback
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.events import EventWriter, rebuild_run_state
from optiprofiler_evolve.projections import project_public_events
from optiprofiler_evolve.viewers import render_status, render_public_report
from optiprofiler_evolve.protocols import StepResult, VariantRequest, WorkerOutcome
from optiprofiler_evolve.solver import InterfaceSpec

from test_config import minimal_config
from test_extensibility import evaluator_factory
from worker_helpers import RunnerAdapter


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

    def checker(self, mode="warn", ratio=1.2):
        return ComplexityChecker(
            self.seed, ComplexityConfig(mode, ratio), SandboxConfig(), self.root / "analysis"
        )

    def test_config_defaults_and_invalid_values(self):
        self.assertIsNone(load_config(minimal_config()).complexity)
        raw = minimal_config()
        raw["complexity"] = {}
        self.assertEqual(load_config(raw).complexity, ComplexityConfig())
        raw["complexity"] = None
        self.assertIsNone(load_config(raw).complexity)
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
        checker = self.checker("enforce", 2)
        self.assertEqual(checker.assess(self.seed).summary()["score"], 100)
        self.assertEqual(checker.assess(self.candidate).status, "within_limit")
        with patch.object(checker, "_analyze", wraps=checker._analyze) as analyzer:
            checker.assess(self.candidate)
            analyzer.assert_not_called()
            (self.candidate / "solver.py").write_text(LARGE + "if True:\n    pass\n")
            report = checker.assess(self.candidate)
            self.assertTrue(report.blocked)
            self.assertEqual(report.baseline_hash, checker.baseline_hash)
            self.assertEqual(analyzer.call_count, 1)

    def test_empty_size_baseline_has_finite_ratio(self):
        (self.seed / "solver.py").write_text("")
        report = self.checker().assess(self.candidate)
        self.assertTrue(math.isfinite(report.summary()["complexity_ratio"]))

    def test_each_metric_can_independently_exceed_the_limit(self):
        checker = self.checker("enforce")
        (self.candidate / "solver.py").write_text(SEED + "extra = 1\n")
        size = checker.assess(self.candidate)
        self.assertEqual(size.ratios, (1, 1.5))
        self.assertTrue(size.blocked)
        (self.candidate / "solver.py").write_text(
            "def solver(fun, x0):\n    return x0 if x0 else None\n"
        )
        branches = checker.assess(self.candidate)
        self.assertEqual(branches.ratios, (2, 1))
        self.assertTrue(branches.blocked)

    def test_invalid_source_has_no_numeric_score_and_baseline_failure_aborts(self):
        checker = self.checker()
        (self.candidate / "helper.py").write_text("def invalid(")
        report = checker.assess(self.candidate)
        self.assertEqual(report.status, "unavailable")
        self.assertIsNone(report.summary()["score"])
        (self.seed / "solver.py").write_text("def invalid(")
        with self.assertRaisesRegex(ValueError, "analysis failed"):
            self.checker()

    def test_public_projection_withholds_private_complexity_details(self):
        report = self.checker().assess(self.candidate)
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
        self.assertEqual(state["run"]["complexity"]["status"], "exceeded")
        page = self.root / "public.html"
        render_status(public, page)
        render_public_report(state, self.root / "PUBLIC_REPORT.md")
        for path in (public, page, self.root / "PUBLIC_REPORT.md"):
            text = path.read_text()
            self.assertNotIn("SECRET", text)
            self.assertNotIn("/private/path", text)
            self.assertNotIn("0.91234", text)
        self.assertIn("Latest source complexity", page.read_text())

    def test_analysis_failures_and_timeouts_follow_mode(self):
        for mode in ("warn", "enforce"):
            checker = self.checker(mode)
            with patch.object(checker, "_analyze", side_effect=ValueError("unavailable")):
                report = checker.assess(self.candidate)
            self.assertEqual(report.status, "unavailable")
            self.assertEqual(report.blocked, mode == "enforce")
            self.assertIsNone(report.summary()["score"])
        with patch(
            "optiprofiler_evolve.complexity.subprocess.run",
            side_effect=subprocess.TimeoutExpired("analyze", 30),
        ):
            with self.assertRaisesRegex(ValueError, "timed out"):
                self.checker()

    def test_analysis_never_executes_source(self):
        marker = self.root / "executed"
        (self.candidate / "solver.py").write_text(
            f"open({str(marker)!r}, 'w').write('bad')\n" + LARGE
        )
        self.checker().assess(self.candidate)
        self.assertFalse(marker.exists())

    def test_all_feedback_modes_include_safe_complexity_details(self):
        report = self.checker().assess(self.candidate)
        source = self.root / "evidence"
        source.mkdir()
        (source / "agent_report.json").write_text("{}")
        result = EvaluationResult("public", 0.5, 0.5, 0.5, 1, source)
        for mode in ("score_only", "structured_numeric", "full_artifacts"):
            destination = self.root / mode
            project_feedback(
                source,
                destination,
                result,
                mode=mode,
                complexity=report,
                validate_agent_report=lambda value: None,
            )
            payload = json.loads((destination / "complexity.json").read_text())
            self.assertNotIn("hotspots", payload["baseline"])
            self.assertNotIn("baseline_hash", payload)
            self.assertIn("Simplify", (destination / "feedback.md").read_text())

    def test_broker_enforcement_quota_and_snapshot(self):
        for mode in ("warn", "enforce"):
            checker = self.checker(mode)
            evaluator = RecordingEvaluator()

            def assess(snapshot):
                report = checker.assess(snapshot)
                (self.candidate / "solver.py").write_text(SEED)
                return report

            (self.candidate / "solver.py").write_text(LARGE)
            broker = EvaluationBroker(
                workspace=self.candidate,
                control_dir=self.root / mode,
                evaluator=evaluator,
                max_smoke_calls=1,
                max_public_calls=1,
                complexity_checker=assess,
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
                    self.assertEqual(payload["payload"]["complexity"]["status"], "exceeded")
                    if mode == "enforce":
                        self.assertFalse(payload["payload"]["evaluated"])
                        self.assertNotIn("score", payload["payload"])
                else:
                    self.assertEqual(payload["payload"]["error"], "evaluation quota exhausted")
            self.assertEqual(len(evaluator.calls), int(mode == "warn"))
            if evaluator.calls:
                self.assertEqual(evaluator.calls[0][2], LARGE)

    def engine(self, mode, ratio=1.2):
        raw = minimal_config()
        raw["complexity"] = {"mode": mode, "max_ratio": ratio}
        raw["evolution"].update(islands=1, attempts_per_island=1)

        def mutate(**kwargs):
            (kwargs["workspace"] / "solver.py").write_text(LARGE)
            kwargs["transcript"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["transcript"].write_text("synthetic mutation")
            return WorkerOutcome(0, kwargs["transcript"])

        return EvolutionEngine(
            initial=self.seed,
            interface=InterfaceSpec.parse("solver.py:solver"),
            runtime="python",
            editable=(".",),
            config=load_config(raw),
            run_dir=self.root / "run",
            worker_adapter=RunnerAdapter(mutate),
            evaluator_factory=evaluator_factory,
        )

    def test_enforced_candidate_never_reaches_performance_or_validation(self):
        engine = self.engine("enforce")
        evaluator = RecordingEvaluator()
        engine.evaluator_factory = lambda **kwargs: evaluator
        engine.run()
        self.assertFalse(engine.candidates["it001-i00-a00"].valid)
        self.assertTrue(all(code == SEED for _, _, code in evaluator.calls))

    def test_warn_candidate_is_admitted_and_reported(self):
        engine = self.engine("warn")
        engine.run()
        self.assertTrue(engine.candidates["it001-i00-a00"].valid)
        self.assertTrue((engine.run_dir / "controller/complexity/final.json").is_file())
        self.assertIn("Source complexity", (engine.run_dir / "FINAL_REPORT.md").read_text())

    def test_strict_reduction_without_eligible_candidate_skips_hidden(self):
        engine = self.engine("enforce", 0.8)
        evaluator = RecordingEvaluator()
        engine.evaluator_factory = lambda **kwargs: evaluator
        with self.assertRaisesRegex(RuntimeError, "acceptance requirements"):
            engine.run()
        self.assertNotIn("hidden", [mode for _, mode, _ in evaluator.calls])

    def test_finalist_cutoff_does_not_hide_eligible_simplification_behind_seed(self):
        engine = self.engine("enforce", 0.8)
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

        engine.worker_adapter = RunnerAdapter(simplify)
        engine.evaluator_factory = lambda **kwargs: PreferSeed()
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

        engine.attempt_steps += (ExpandAfterScore(),)
        engine.run()
        self.assertFalse(engine.candidates["it001-i00-a00"].valid)

    def test_research_variant_uses_same_limit(self):
        engine = self.engine("enforce")
        engine._initialize_run_directory()
        engine._phase_prepare()
        variant = self.source("run/research/proposed", LARGE)
        with self.assertRaises(ComplexityLimitExceeded):
            engine.controller_services.materialize_variant(
                VariantRequest("large", engine.run_dir / "candidates/seed", tree=variant)
            )
        self.assertNotIn("large", engine.variant_handles)
