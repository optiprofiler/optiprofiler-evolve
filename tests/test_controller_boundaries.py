from __future__ import annotations

import dataclasses
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_config import minimal_config
from test_extensibility import agent_runner, evaluator_factory
from test_owner_views import ATTEMPT, _attempt_rows, _write_events
from test_selection import candidate
from worker_helpers import RunnerAdapter

from optiprofiler_evolve.config import ComponentConfig, WorkerConfig, load_config
from optiprofiler_evolve.controller.engine import EvolutionEngine
from optiprofiler_evolve.controller.registry import build
from optiprofiler_evolve.controller.selection import (
    MetricParetoRetention,
    MetricSelectionError,
    TopBiasedValidationWeightedSampler,
    ValidationLexicographicRetention,
)
from optiprofiler_evolve.models import MetricBundle, MetricValue
from optiprofiler_evolve.observability.views.owner import render_owner_views
from optiprofiler_evolve.phases.attempts.contracts import PopulationEdit, StepResult
from optiprofiler_evolve.solver import InterfaceSpec


class RepairAfterEvaluation:
    name = "repair_after_evaluation"

    def run(self, context) -> StepResult:
        path = context.workspace / "solver.py"
        path.write_text(path.read_text().replace("improved", "changed"))
        return StepResult(verdict="repaired")


class ControllerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        source.mkdir()
        (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
        config = load_config(minimal_config())
        config = dataclasses.replace(
            config,
            evolution=dataclasses.replace(
                config.evolution, islands=1, iterations=1, attempts_per_island=1
            ),
        )
        self.engine = EvolutionEngine(
            initial=source,
            interface=InterfaceSpec.parse("solver.py:solver"),
            runtime="python",
            editable=(".",),
            config=config,
            run_dir=self.root / "run",
            worker_adapter=RunnerAdapter(agent_runner),
            evaluator_factory=evaluator_factory,
        )

    def test_startup_failure_closes_threads(self) -> None:
        threads = []

        def fail(_workers):
            threads.extend((self.engine.events._thread, self.engine._refresh_thread))
            raise RuntimeError("provenance unavailable")

        with patch.object(self.engine.worker_adapter, "provenance", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "provenance unavailable"):
                self.engine.run()
        self.assertIsNone(self.engine.events)
        self.assertTrue(threads)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_repair_invalidates_public_score_and_preserves_both_evaluations(
        self,
    ) -> None:
        self.engine.attempt_steps += (RepairAfterEvaluation(),)
        self.engine.run()
        record = self.engine.candidates[ATTEMPT]
        self.assertTrue(record.valid)
        self.assertEqual(record.public_score, 0.5)
        self.assertEqual(record.validation_score, 0.5)
        output = self.engine.run_dir / "controller" / "evaluations" / ATTEMPT
        self.assertTrue((output / "public" / "result.json").is_file())
        self.assertTrue((output / "public-002" / "result.json").is_file())

    def test_population_edits_are_atomic_and_reject_unapproved_records(self) -> None:
        a = dataclasses.replace(candidate("a", 0.7, 0.7), review_verdict="approve")
        b = dataclasses.replace(candidate("b", 0.8, 0.8), review_verdict="approve")
        bad = dataclasses.replace(candidate("bad", 0.9, 0.9), valid=False)
        unreviewed = dataclasses.replace(candidate("unreviewed", 0.9, 0.9), assessment_score=None)
        self.engine.candidates = {r.candidate_id: r for r in (a, b, bad, unreviewed)}
        edits = (
            PopulationEdit(kill=("a",), migrate=(("unknown", 0),)),
            PopulationEdit(migrate=(("b", 0), ("unknown", 0))),
            PopulationEdit(migrate=(("bad", 0),)),
            PopulationEdit(migrate=(("unreviewed", 0),)),
            PopulationEdit(kill=("a",), migrate=(("a", 0),)),
        )
        for edit in edits:
            with self.subTest(edit=edit):
                self.engine.populations = [[a]]
                with self.assertRaises(ValueError):
                    self.engine._apply_population_edit(edit)
                self.assertEqual(self.engine.populations, [[a]])
        self.engine._apply_population_edit(PopulationEdit(migrate=(("b", 0),)))
        self.assertEqual(self.engine.populations, [[b, a]])

    def test_core_phase_preserves_internal_key_error(self) -> None:
        from optiprofiler_evolve.phases.prepare import PreparePhase

        with patch.object(
            PreparePhase, "run", side_effect=KeyError("missing_field")
        ), self.assertRaisesRegex(KeyError, "missing_field"):
            self.engine._run_phase(PreparePhase(), {})

    def test_weighted_worker_selection_preserves_seeded_choices(self) -> None:
        pool = tuple(
            WorkerConfig("codex", f"model-{weight}", weight=weight)
            for weight in (1, 3, 7)
        )
        self.engine.config = dataclasses.replace(
            self.engine.config,
            workers=dataclasses.replace(self.engine.config.workers, pool=pool),
        )
        expanded = [worker for worker in pool for _ in range(worker.weight)]
        for seed in range(100):
            self.assertEqual(
                self.engine._select_worker(random.Random(seed)),
                random.Random(seed).choice(expanded),
            )

    def test_selection_components_accept_instances_and_classes(self) -> None:
        for kind, cls in (
            ("retention", ValidationLexicographicRetention),
            ("sampler", TopBiasedValidationWeightedSampler),
        ):
            with self.subTest(kind=kind):
                component = cls()
                self.assertIs(
                    build(kind, ComponentConfig.from_component(component)), component
                )
                self.assertIsInstance(
                    build(kind, ComponentConfig.from_component(cls)), cls
                )

    def test_single_pareto_objective_obeys_direction_and_requires_metrics(self) -> None:
        def bundle(fitness, cost):
            return MetricBundle(
                "fitness",
                "fixed",
                (
                    MetricValue("fitness", fitness, "max", "score"),
                    MetricValue("cost", cost, "min", "cost"),
                ),
            )

        expensive = candidate("expensive", 0.9, 0.9, metrics=bundle(0.9, 100))
        cheap = candidate("cheap", 0.1, 0.1, metrics=bundle(0.1, 1))
        policy = MetricParetoRetention(objectives=("cost",))
        self.assertEqual(policy.retain((expensive, cheap), 1), (cheap,))
        with self.assertRaises(MetricSelectionError):
            MetricParetoRetention(objectives=("unknown",)).retain((expensive,), 1)
        with self.assertRaises(MetricSelectionError):
            policy.retain((candidate("missing", 0.8, 0.8),), 1)

    def test_owner_pages_refresh_once_on_terminal_transition(self) -> None:
        ledger = self.root / "events.jsonl"
        _write_events(ledger, _attempt_rows(self.root, terminal=False))
        render_owner_views(ledger, self.root)
        page = self.root / "owner" / "attempts" / f"{ATTEMPT}.html"
        running = page.read_text()
        _write_events(ledger, _attempt_rows(self.root, terminal=True))
        render_owner_views(ledger, self.root)
        self.assertNotEqual(running, page.read_text())
        mtime = page.stat().st_mtime_ns
        render_owner_views(ledger, self.root)
        self.assertEqual(mtime, page.stat().st_mtime_ns)
