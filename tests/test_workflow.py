from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from test_config import minimal_config
from test_engine import FakeEvaluator, fake_agent_runner, fake_evaluator_factory
from worker_helpers import RunnerAdapter

from optiprofiler_evolve import evolve
from optiprofiler_evolve.application.run import EvolutionRun
from optiprofiler_evolve.config import ComponentConfig, load_config
from optiprofiler_evolve.controller.contracts import VariantRequest
from optiprofiler_evolve.controller.registry import build
from optiprofiler_evolve.errors import RunCancelled
from optiprofiler_evolve.phases.attempts.contracts import PopulationEdit
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult, validate_workflow
from optiprofiler_evolve.phases.hidden import HiddenPhase, HiddenResult
from optiprofiler_evolve.phases.prepare import (
    PrepareContext,
    PreparedArtifact,
    PreparePhase,
    PrepareResult,
)
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
    def test_core_names_do_not_grant_core_contexts(self):
        class OrdinaryPhase(ArtifactPhase):
            def run(inner, context):
                self.assertIs(type(context), PhaseContext)
                return super().run(context)

        for name in ("prepare", "explore", "validate", "hidden", "report"):
            with self.subTest(name=name):
                result = self.make_run((OrdinaryPhase(name),), name=name).run()
                self.assertEqual(dict(result.artifacts), {"custom": "custom"})
        self.evaluator.assert_not_called()
        self.worker.assert_not_called()

    def test_renamed_core_subclass_retains_its_context(self):
        class RenamedPrepare(PreparePhase):
            name = "renamed_prepare"

            def run(inner, context):
                self.assertIsInstance(context, PrepareContext)
                return super().run(context)

        result = self.make_run((RenamedPrepare(),)).run()
        self.assertIn("prepared", result.artifacts)

    def test_duplicate_outputs_fail_before_creating_run(self):
        with self.assertRaisesRegex(ValueError, "duplicate artifacts.*shared"):
            self.make_run((
                ArtifactPhase("first", provides=("shared",)),
                ArtifactPhase("second", requires=("shared",), provides=("shared",)),
            ))
        self.assertFalse((self.root / "run").exists())
        self.evaluator.assert_not_called()

    def test_result_supports_copy_asdict_and_hash_with_mutable_artifacts(self):
        result = self.make_run(()).run()
        result = dataclasses.replace(result, artifacts={"nested": {"values": [1]}})
        copied = deepcopy(result)
        converted = dataclasses.asdict(result)
        self.assertEqual(copied, result)
        self.assertEqual(hash(copied), hash(result))
        copied.artifacts["nested"]["values"].append(2)
        converted["artifacts"]["nested"]["values"].append(3)
        self.assertEqual(result.artifacts["nested"]["values"], [1])
        with self.assertRaises(TypeError):
            copied.artifacts["extra"] = True

    def test_prepared_island_count_must_match_before_population_commit(self):
        class InconsistentPrepare(PreparePhase):
            def run(inner, context):
                result = super().run(context)
                return dataclasses.replace(
                    result, prepared=dataclasses.replace(result.prepared, islands=999),
                )

        run = self.make_run((InconsistentPrepare(),))
        with self.assertRaisesRegex(ValueError, "island count"):
            run.run()
        self.assertEqual(run.state.candidates, {})
        self.assertEqual(run.state.populations, [])

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
                run.commits.commit_phase_result(PhaseResult(artifacts={"later": True}))
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
        self.assertTrue(run.state.hidden_started)
        self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)
        fake_final = ArtifactPhase("fake_final", requires=("champion",), provides=("final",))
        with self.assertRaisesRegex(ValueError, "required lockbox"):
            self.make_run(
                ("prepare", "explore", "validate", fake_final), config=config, name="fake-final",
            ).run()
        self.assertFalse((self.root / "fake-final").exists())

    def test_research_requirements_are_owned_by_phase_hooks(self):
        config = self.config.with_criterion("performance", enabled=False)
        for name in ("strategy_analysis", "strategy_transfer", "recombine", "challenger"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "public performance"):
                build("phase", ComponentConfig(name)).validate_config(config)

    def test_lockbox_uses_committed_core_results_not_custom_artifact_names(self):
        class SelectCandidate(ArtifactPhase):
            def run(self, context):
                return ValidateResult(champion=(0, context.artifacts["populations"][0][0]))

        raw = minimal_config()
        raw["data"]["split"]["lockbox_required"] = True
        config = load_config(raw)
        names_only = ArtifactPhase("names_only", provides=("champion", "final"))
        result = self.make_run((names_only,), config=config, name="names-only").run()
        self.assertIsNone(result.best_candidate_id)
        self.assertEqual(set(result.artifacts), {"champion", "final"})

        selection = SelectCandidate("custom_selection", requires=("populations",), provides=("champion",))
        run = self.make_run(("prepare", "explore", selection), config=config, name="custom-selection")
        with self.assertRaisesRegex(ValueError, "required lockbox"):
            run.run()
        self.assertIsNotNone(run.state.champion)
        self.assertNotIn("hidden", FakeEvaluator.calls)

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

    def test_hidden_checks_tree_changed_by_a_configured_intervening_phase(self):
        class ChangeChampion(ArtifactPhase):
            def run(self, context):
                champion = context.artifacts["champion"][1]
                (champion.path / "solver.py").write_text("changed", encoding="utf-8")
                return PhaseResult()

        change = ChangeChampion("change", requires=("champion",), provides=())
        run = self.make_run(("prepare", "explore", "validate", change, "hidden"))
        with self.assertRaisesRegex(ValueError, "Champion changed"):
            run.run()
        self.assertFalse(run.state.hidden_started)
        self.assertNotIn("hidden", FakeEvaluator.calls)

    def test_failed_holdout_consumes_single_query_and_publishes_nothing(self):
        class RetryHidden(HiddenPhase):
            def run(self, context):
                try:
                    return super().run(context)
                except RuntimeError:
                    return super().run(context)

        run = self.make_run(("prepare", "explore", "validate", RetryHidden()))
        evaluate = FakeEvaluator.evaluate

        def fail_hidden(evaluator, candidate, mode, output_dir):
            if mode == "hidden":
                evaluator.calls.append(mode)
                raise OSError("hidden failed")
            return evaluate(evaluator, candidate, mode, output_dir)

        with patch.object(FakeEvaluator, "evaluate", fail_hidden):
            with self.assertRaisesRegex(RuntimeError, "already been started"):
                run.run()
        self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)
        self.assertTrue(run.state.hidden_started)
        self.assertIsNone(run.state.final_result)
        self.assertNotIn("final", run.state.artifacts)
        marker = run.run_dir / "controller" / "hidden_started.json"
        self.assertEqual(json.loads(marker.read_text())["candidate_id"], run.state.champion[1].candidate_id)
        # Losing the in-memory flag must not authorize a second query in the same directory.
        fresh_state = dataclasses.replace(run.state, hidden_started=False)
        commits = type(run.commits)(
            fresh_state, run.config, run.run_dir, Mock(), Mock(), Mock(),
        )
        with self.assertRaisesRegex(RuntimeError, "already been started"):
            commits.begin_hidden()
        run.commits.write_checkpoint(1)
        self.assertTrue(json.loads((run.run_dir / "state.json").read_text())["hidden_started"])

    def test_successful_holdout_cannot_be_repeated(self):
        class RepeatHidden(HiddenPhase):
            def run(self, context):
                super().run(context)
                return super().run(context)

        run = self.make_run(("prepare", "explore", "validate", RepeatHidden()))
        with self.assertRaisesRegex(RuntimeError, "already been started"):
            run.run()
        self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)

    def test_hidden_does_not_execute_if_consumption_cannot_be_persisted(self):
        run = self.make_run(("prepare", "explore", "validate", "hidden"))
        with patch("optiprofiler_evolve.controller.commits.os.fsync", side_effect=OSError("disk failed")):
            with self.assertRaisesRegex(OSError, "disk failed"):
                run.run()
        self.assertNotIn("hidden", FakeEvaluator.calls)
        self.assertFalse(run.state.hidden_started)
        with self.assertRaisesRegex(RuntimeError, "already been started"):
            run.commits.begin_hidden()

    def test_preflight_does_not_sort_or_invent_missing_producers(self):
        producer = ArtifactPhase("producer", provides=("evidence",))
        consumer = ArtifactPhase("consumer", requires=("evidence",))
        with self.assertRaisesRegex(ValueError, "missing artifacts.*evidence"):
            validate_workflow((consumer, producer), self.config)
        validate_workflow((producer, consumer), self.config)

    def test_late_optional_producer_fails_before_directory_or_worker(self):
        with self.assertRaisesRegex(ValueError, "directions.*producers must run before"):
            self.make_run(("prepare", "explore", "direction_scout", "validate", "hidden", "report"))
        self.assertFalse((self.root / "run").exists())
        self.worker.assert_not_called()
        self.evaluator.assert_not_called()

    def test_optional_producer_absent_or_earlier_is_valid(self):
        producer = ArtifactPhase("producer", provides=("absent",))
        producer.optional_requires = frozenset()
        consumer = ArtifactPhase("consumer")
        validate_workflow((consumer,), self.config)
        validate_workflow((producer, consumer), self.config)
        with self.assertRaisesRegex(ValueError, "optional artifacts"):
            validate_workflow((consumer, producer), self.config)

    def test_core_results_derive_artifacts_and_state_has_no_duplicate_fields(self):
        run = self.make_run(("prepare", "explore", "validate", "hidden"))
        run.run()
        fields = {item.name for item in dataclasses.fields(run.state)}
        self.assertTrue(fields.isdisjoint({"champion", "final_result", "final_solver", "artifacts"}))
        for result in run.state.phase_results:
            if isinstance(result, PrepareResult):
                self.assertIs(result.artifacts["prepared"], result.prepared)
            elif isinstance(result, ValidateResult):
                self.assertIs(result.artifacts["champion"], result.champion)
                self.assertIs(run.state.champion, result.champion)
            elif isinstance(result, HiddenResult):
                self.assertIs(result.artifacts["final"], result.final_result)
                self.assertIs(run.state.final_solver, result.final_solver)
            else:
                continue
            with self.assertRaises((ValueError, TypeError)):
                dataclasses.replace(result, artifacts={"forged": True})
            with self.assertRaises(TypeError):
                result.artifacts["forged"] = True
        with self.assertRaises(AttributeError):
            run.state.champion = None
        with self.assertRaises(TypeError):
            run.state.artifacts["champion"] = None

    def test_configured_late_research_phase_cannot_select_or_materialize(self):
        class LateResearch(ArtifactPhase):
            def __init__(self, operation):
                super().__init__("late_research", requires=("champion",), provides=())
                self.operation = operation

            def run(self, context):
                champion = context.artifacts["champion"][1]
                if self.operation == "select":
                    context.services.select_by_validation((champion.candidate_id,), 1)
                else:
                    context.services.materialize_variant(VariantRequest("late", champion.path))
                return PhaseResult()

        for operation in ("select", "materialize"):
            run = self.make_run(
                ("prepare", "explore", "validate", LateResearch(operation)), name=operation,
            )
            with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "Champion is fixed"):
                run.run()
            self.assertFalse((run.run_dir / "research" / "variant_staging").exists())
            self.assertEqual(run.state.validation_selection_count, 0)

    def test_return_integrity_checks_export_not_its_old_candidate_copy(self):
        class ChangeTree(ArtifactPhase):
            def __init__(self, exported):
                super().__init__("change_tree", requires=("champion", "final_solver"), provides=())
                self.exported = exported

            def run(self, context):
                path = (context.artifacts["final_solver"] if self.exported
                        else context.artifacts["champion"][1].path)
                (path / "solver.py").write_text("changed", encoding="utf-8")
                return PhaseResult()

        original = self.make_run(
            ("prepare", "explore", "validate", "hidden", ChangeTree(False)), name="old-copy",
        ).run()
        self.assertTrue(original.best_solver.is_dir())
        with self.assertRaisesRegex(ValueError, "Returned solver changed"):
            self.make_run(
                ("prepare", "explore", "validate", "hidden", ChangeTree(True)), name="export",
            ).run()
