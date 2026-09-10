from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_config import minimal_config
from test_engine import FakeEvaluator, fake_agent_runner, fake_evaluator_factory
from worker_helpers import RunnerAdapter

from optiprofiler_evolve import evolve
from optiprofiler_evolve.application.run import EvolutionRun
from optiprofiler_evolve.config import ComponentConfig, load_config
from optiprofiler_evolve.controller.coordinator import RunCoordinator
from optiprofiler_evolve.controller.registry import build
from optiprofiler_evolve.errors import RunCancelled
from optiprofiler_evolve.phases.attempts.contracts import PopulationEdit
from optiprofiler_evolve.phases.contracts import PhaseResult
from optiprofiler_evolve.phases.hidden import HiddenPhase
from optiprofiler_evolve.phases.prepare import PreparedArtifact
from optiprofiler_evolve.phases.validate import ValidateResult
from optiprofiler_evolve.solver import InterfaceSpec


class ArtifactPhase:
    def __init__(self, name="custom", *, requires=(), provides=("custom",), status="succeeded"):
        self.name = name
        self.requires = frozenset(requires)
        self.provides = frozenset(provides)
        self.optional_requires = frozenset({"absent"})
        self.status = status
        self.inputs = None

    def run(self, context):
        self.inputs = context.artifacts
        return PhaseResult(status=self.status, artifacts={name: name for name in self.provides})


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "solver.py"
        self.source.write_text("def solver(fun, x0):\n    return x0\n", encoding="utf-8")
        self.config = load_config(minimal_config())
        self.worker = Mock(side_effect=fake_agent_runner)
        self.evaluator = Mock(side_effect=fake_evaluator_factory)
        FakeEvaluator.calls = []

    def make_run(self, phases=None, *, config=None, name="run"):
        config = config or self.config
        if phases is not None:
            specs = tuple(
                ComponentConfig(phase) if isinstance(phase, str)
                else ComponentConfig.from_component(phase)
                for phase in phases
            )
            config = dataclasses.replace(
                config, workflow=dataclasses.replace(config.workflow, phases=specs)
            )
        return EvolutionRun(
            initial=self.source,
            interface=InterfaceSpec.parse("solver.py:solver"),
            runtime="python",
            editable=(".",),
            config=config,
            run_dir=self.root / name,
            worker_adapter=RunnerAdapter(self.worker),
            evaluator_factory=self.evaluator,
        )

    def test_empty_and_ordinary_custom_pipelines_return_artifacts_without_a_winner(self):
        producer = ArtifactPhase("produce", provides=("evidence",))
        consumer = ArtifactPhase("consume", requires=("evidence",), provides=("summary",))
        for index, phases in enumerate(((), (producer,), (producer, consumer))):
            with self.subTest(phases=phases):
                run = self.make_run(phases, name=f"run-{index}")
                result = run.run()
                for field in (
                    "best_solver", "best_candidate_id", "public_score", "validation_score",
                    "final_score", "assessment_score", "final_assessment_score",
                ):
                    self.assertIsNone(getattr(result, field))
                self.assertEqual(result.finalists, ())
                self.assertEqual(set(result.artifacts), set().union(*(p.provides for p in phases)))
                with self.assertRaises(TypeError):
                    result.artifacts["extra"] = True
                run.state.artifacts["later"] = True
                self.assertNotIn("later", result.artifacts)
                self.assertFalse((result.run_dir / "FINAL_REPORT.md").exists())
        self.assertEqual(dict(consumer.inputs), {"evidence": "evidence"})
        self.worker.assert_not_called()
        self.evaluator.assert_not_called()

    def test_dependency_failure_is_before_infrastructure_or_directory_creation(self):
        with patch("optiprofiler_evolve.application.run.resolve") as resolve:
            with self.assertRaisesRegex(ValueError, "missing artifacts.*prepared"):
                self.make_run(("explore",))
            resolve.assert_not_called()
        self.assertFalse((self.root / "run").exists())
        self.worker.assert_not_called()
        self.evaluator.assert_not_called()

    def test_preflight_checks_resolved_names_not_only_config_aliases(self):
        config = dataclasses.replace(self.config, workflow=dataclasses.replace(
            self.config.workflow,
            phases=tuple(ComponentConfig.from_component(ArtifactPhase(), name=name)
                         for name in ("first_alias", "second_alias")),
        ))
        with self.assertRaisesRegex(ValueError, "Duplicate phase"):
            self.make_run(config=config)

    def test_phase_config_hook_uses_constructed_options_before_run(self):
        class CheckedPhase(ArtifactPhase):
            def __init__(self, *, limit):
                super().__init__()
                self.limit = limit

            def validate_config(self, config):
                if self.limit > config.evolution.islands:
                    raise ValueError("limit exceeds islands")

        spec = ComponentConfig("custom", {"limit": 100}, _factory=CheckedPhase)
        config = dataclasses.replace(self.config, workflow=dataclasses.replace(
            self.config.workflow, phases=(spec,),
        ))
        with self.assertRaisesRegex(ValueError, "limit exceeds islands"):
            self.make_run(config=config)
        self.assertFalse((self.root / "run").exists())

    def test_attempt_steps_are_only_constructed_for_attempt_workflows(self):
        for index, steps in enumerate(((), (ComponentConfig("not_installed"),))):
            config = dataclasses.replace(self.config, workflow=dataclasses.replace(
                self.config.workflow, attempt_steps=steps,
            ))
            run = self.make_run((), config=config, name=f"unused-steps-{index}")
            self.assertEqual(run.attempt_steps, ())
            run.run()
            with self.assertRaisesRegex(ValueError, "attempt_steps must include"):
                self.make_run(("prepare", "explore"), config=config)

    def test_attempt_entry_rejects_missing_steps_before_work(self):
        run = self.make_run(())
        with self.assertRaisesRegex(ValueError, "attempt_steps must include"):
            run.attempt_runner.run(
                iteration=1, island=0, attempt_index=0, parent=Mock(), worker=Mock(),
            )
        self.assertFalse(run.run_dir.exists())

    def test_prepared_seed_is_not_implicitly_returned_as_champion(self):
        result = self.make_run(("prepare",)).run()
        self.assertIsInstance(result.artifacts["prepared"], PreparedArtifact)
        self.assertIsInstance(result.artifacts["prepared"].seed, Path)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.artifacts["prepared"].islands = 7
        self.assertIsNone(result.best_solver)
        self.assertIsNone(result.public_score)
        self.worker.assert_not_called()

    def test_public_api_supports_no_evaluator_without_test_adapter_injection(self):
        config = self.config.with_criterion("performance", enabled=False).with_criterion(
            "integrity", enabled=False,
        ).with_criterion("complexity", enabled=True)
        config = dataclasses.replace(config, workflow=dataclasses.replace(
            config.workflow, phases=(ComponentConfig.from_component(ArtifactPhase()),),
        ))
        with patch("optiprofiler_evolve.agents.workers.CliWorkerAdapter.run") as worker:
            result = evolve(self.source, config=config, run_dir=self.root / "no-evaluator")
            worker.assert_not_called()
        provenance = json.loads((result.run_dir / "provenance.json").read_text())
        self.assertIsNone(provenance["components"]["evaluator"])
        self.assertEqual(provenance["components"]["attempt_steps"], [])
        self.assertEqual(dict(result.artifacts), {"custom": "custom"})

    def test_champion_without_hidden_or_report_has_no_fabricated_final(self):
        run = self.make_run(("prepare", "explore", "validate"))
        result = run.run()
        champion = run.state.champion[1]
        self.assertEqual(result.best_solver, champion.path)
        self.assertEqual(result.best_candidate_id, champion.candidate_id)
        self.assertEqual(result.public_score, champion.public_score)
        self.assertEqual(result.validation_score, champion.validation_score)
        self.assertEqual(result.assessment_score, champion.assessment_score)
        self.assertIsNone(result.final_score)
        self.assertIsNone(result.final_assessment_score)
        self.assertEqual(result.finalists, ())
        self.assertNotIn("hidden", FakeEvaluator.calls)

    def test_hidden_without_report_returns_export_and_one_finalist(self):
        result = self.make_run(("prepare", "explore", "validate", "hidden")).run()
        self.assertEqual(result.best_solver, result.run_dir / "final_solver")
        self.assertEqual(result.artifacts["final_solver"], result.best_solver)
        self.assertEqual(len(result.finalists), 1)
        self.assertIsNotNone(result.final_score)
        self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)
        self.assertNotIn("report", result.artifacts)
        self.assertFalse((result.run_dir / "FINAL_REPORT.md").exists())

    def test_report_is_only_evidence_and_default_workflow_order_is_unchanged(self):
        run = self.make_run()
        result = run.run()
        self.assertEqual([p.name for p in run.phases],
                         ["prepare", "explore", "validate", "hidden", "report"])
        self.assertTrue(result.artifacts["report"].is_file())
        self.assertNotIn("result", result.artifacts)
        self.assertFalse(hasattr(run.state, "result"))

    def test_required_lockbox_cannot_be_removed_or_disabled(self):
        raw = minimal_config()
        raw["data"]["split"]["lockbox_required"] = True
        config = load_config(raw)
        with self.assertRaisesRegex(ValueError, "required lockbox"):
            self.make_run(("prepare", "explore", "validate"), config=config)
        self.assertFalse((self.root / "run").exists())
        # Metadata-only pipelines do not claim a champion or spend a holdout.
        self.make_run((), config=config)
        disabled = dataclasses.replace(config, assessment=dataclasses.replace(
            config.assessment, profiles={"hidden": {"performance": {"enabled": False}}},
        ))
        with self.assertRaisesRegex(ValueError, "required lockbox"):
            self.make_run(config=disabled)
        run = self.make_run(config=config)
        run.run()
        self.assertTrue(run.state.holdout_started)
        self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)
        fake_final = ArtifactPhase("fake_final", requires=("champion",), provides=("final",))
        with self.assertRaisesRegex(RuntimeError, "required lockbox"):
            self.make_run(
                ("prepare", "explore", "validate", fake_final), config=config, name="fake-final",
            ).run()

    def test_research_requirements_are_owned_by_phase_hooks(self):
        config = self.config.with_criterion("performance", enabled=False)
        for name in ("strategy_analysis", "strategy_transfer", "recombine", "challenger"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "public performance"):
                build("phase", ComponentConfig(name)).validate_config(config)

    def test_failures_and_cancellation_are_not_converted_into_partial_success(self):
        for status, error in (("failed", RuntimeError), ("cancelled", RunCancelled)):
            run = self.make_run((ArtifactPhase(status=status),), name=status)
            with self.subTest(status=status), self.assertRaises(error):
                run.run()
            self.assertEqual(run.state.artifacts, {})

    def test_champion_freezes_all_admission_and_selection_entries(self):
        run = self.make_run(("prepare", "explore", "validate"))
        run.run()
        champion = run.state.champion
        commits = run.commits
        actions = (
            lambda: commits.commit_phase_result(ValidateResult(champion=champion)),
            lambda: commits.accept_and_record(Mock()),
            lambda: commits.register_research_finalist(champion[1]),
            lambda: commits.apply_population_edit(PopulationEdit()),
            lambda: commits.begin_validation_selection(),
            lambda: commits.record_variant(Mock(), self.source),
            lambda: commits.record_public_evaluation("candidate", Mock()),
            lambda: commits.record_validation_evaluation("candidate", Mock()),
            lambda: run.controller_services.select_by_validation((champion[1].candidate_id,), 1),
            lambda: run.controller_services.materialize_variant(Mock()),
            lambda: run.controller_services.register_finalist(Mock(), {}),
            lambda: run.attempt_runner.run(
                iteration=2, island=0, attempt_index=0, parent=champion[1], worker=Mock(),
            ),
        )
        for action in actions:
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "Champion is fixed"):
                action()
        self.assertIs(run.state.champion, champion)

    def test_holdout_checks_champion_identity_and_hash_before_query(self):
        run = self.make_run(("prepare", "explore", "validate"))
        run.run()
        island, champion = run.state.champion
        forged = dataclasses.replace(champion, candidate_id="other")
        with self.assertRaisesRegex(ValueError, "fixed champion"):
            run.commits.begin_holdout((island, forged))
        self.assertFalse(run.state.holdout_started)
        (champion.path / "solver.py").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Champion changed"):
            run.coordinator.run_phase(HiddenPhase(), {})
        self.assertFalse(run.state.holdout_started)
        self.assertNotIn("hidden", FakeEvaluator.calls)

    def test_failed_holdout_consumes_single_query_and_publishes_nothing(self):
        run = self.make_run(("prepare", "explore", "validate"))
        run.run()
        with patch.object(run.state.evaluator, "evaluate", side_effect=OSError("holdout failed")) as call:
            with self.assertRaisesRegex(OSError, "holdout failed"):
                run.coordinator.run_phase(HiddenPhase(), {})
            with self.assertRaisesRegex(RuntimeError, "already been started"):
                run.coordinator.run_phase(HiddenPhase(), {})
            call.assert_called_once()
        self.assertTrue(run.state.holdout_started)
        self.assertIsNone(run.state.final_result)
        self.assertNotIn("final", run.state.artifacts)

    def test_successful_holdout_cannot_be_repeated(self):
        run = self.make_run(("prepare", "explore", "validate", "hidden"))
        run.run()
        with self.assertRaisesRegex(RuntimeError, "already been started"):
            run.coordinator.run_phase(HiddenPhase(), {})
        self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)

    def test_preflight_does_not_sort_or_invent_missing_producers(self):
        producer = ArtifactPhase("producer", provides=("evidence",))
        consumer = ArtifactPhase("consumer", requires=("evidence",))
        with self.assertRaisesRegex(ValueError, "missing artifacts.*evidence"):
            RunCoordinator.preflight((consumer, producer), self.config)
        RunCoordinator.preflight((producer, consumer), self.config)
