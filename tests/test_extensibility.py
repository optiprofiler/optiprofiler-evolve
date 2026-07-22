from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.config import ComponentConfig, load_config
from optiprofiler_evolve.engine import EvolutionEngine
from optiprofiler_evolve.models import CandidateRecord, EvaluationResult
from optiprofiler_evolve.policies.builtin import MigrationPolicy
from optiprofiler_evolve.protocols import (
    ControllerServices,
    IterationView,
    PopulationEdit,
    StepResult,
)
from optiprofiler_evolve.registry import build
from optiprofiler_evolve.sandbox import AgentRunResult
from optiprofiler_evolve.solver import InterfaceSpec

from test_config import minimal_config


class FakeEvaluator:
    name = "fake"
    deterministic = True

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        improved = "improved" in (candidate / "solver.py").read_text(encoding="utf-8")
        score = 0.7 if improved else 0.5
        result = EvaluationResult(mode, score, score, 0.5, 1, output_dir)
        (output_dir / "result.json").write_text(json.dumps(result.as_dict(), default=str))
        (output_dir / "feedback.md").write_text("feedback", encoding="utf-8")
        return result


def evaluator_factory(**_kwargs: object) -> FakeEvaluator:
    return FakeEvaluator()


def agent_runner(**kwargs: object) -> AgentRunResult:
    workspace = kwargs["workspace"]
    transcript = kwargs["transcript"]
    assert isinstance(workspace, Path)
    assert isinstance(transcript, Path)
    solver = workspace / "solver.py"
    solver.write_text(solver.read_text(encoding="utf-8") + "\n# improved\n")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("worker", encoding="utf-8")
    return AgentRunResult(0, transcript)


def out_of_scope_runner(**kwargs: object) -> AgentRunResult:
    workspace = kwargs["workspace"]
    transcript = kwargs["transcript"]
    assert isinstance(workspace, Path)
    assert isinstance(transcript, Path)
    (workspace / "locked.txt").write_text("changed", encoding="utf-8")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("worker", encoding="utf-8")
    return AgentRunResult(0, transcript)


class InspectStep:
    name = "inspect_context"
    seen: set[str] = set()

    def run(self, context: object) -> StepResult:
        self.__class__.seen = set(dir(context))
        return StepResult(metrics={"custom_step": True})


class NestedOptionsStep:
    name = "nested_options"

    def __init__(self, *, tools: object) -> None:
        self.tools = tools

    def run(self, _context: object) -> StepResult:
        return StepResult()


class StopAfterFirstIteration:
    name = "stop_after_first"

    def propose(self, view: object) -> PopulationEdit:
        return PopulationEdit(stop=getattr(view, "iteration") >= 1, reason="test")


class UnsupportedBudgetPolicy:
    name = "unsupported_budget"

    def propose(self, _view: object) -> PopulationEdit:
        return PopulationEdit(budget={"extra_attempts": 1})


class SeedCaptureStep:
    name = "seed_capture"

    def __init__(self) -> None:
        self.seeds: dict[str, int] = {}

    def run(self, context: object) -> StepResult:
        self.seeds[getattr(context, "attempt_id")] = getattr(context, "rng_seed")
        return StepResult()


class ExtensibilityTests(unittest.TestCase):
    def test_frozen_config_helpers_create_isolated_ablation_variants(self) -> None:
        base = load_config(minimal_config())
        without = base.without_step("static_audit")
        reordered = base.reorder_steps(
            ["mutate", "smoke", "public_evaluate", "feedback", "static_audit"]
        )
        inserted = base.with_step(InspectStep(), after="static_audit")

        self.assertIn("static_audit", [step.name for step in base.workflow.attempt_steps])
        self.assertNotIn("static_audit", [step.name for step in without.workflow.attempt_steps])
        self.assertEqual(reordered.workflow.attempt_steps[-1].name, "static_audit")
        self.assertIn("inspect_context", [step.name for step in inserted.workflow.attempt_steps])
        with self.assertRaises(TypeError):
            base.evaluation.benchmark["new"] = True  # type: ignore[index]

    def test_phase_options_are_immutable_ablation_variants(self) -> None:
        base = load_config(minimal_config()).with_phase(
            ComponentConfig("direction_scout", {"mode": "shared"}),
            after="prepare",
        )
        disabled = base.with_phase_options("direction_scout", mode="off")
        removed = base.without_phase("direction_scout")

        self.assertEqual(base.workflow.phases[1].options["mode"], "shared")
        self.assertEqual(disabled.workflow.phases[1].options["mode"], "off")
        self.assertNotIn("direction_scout", [phase.name for phase in removed.workflow.phases])
        with self.assertRaisesRegex(ValueError, "Core phase"):
            base.without_phase("explore")

    def test_direction_scout_modes_form_independent_config_variants(self) -> None:
        base = load_config(minimal_config()).with_phase(
            ComponentConfig("direction_scout", {"mode": "shared"}),
            after="prepare",
        )

        variants = {
            mode: base.with_phase_options("direction_scout", mode=mode)
            for mode in ("off", "shared", "per_island")
        }

        self.assertEqual(
            {mode: config.workflow.phases[1].options["mode"] for mode, config in variants.items()},
            {"off": "off", "shared": "shared", "per_island": "per_island"},
        )
        self.assertEqual(base.workflow.phases[1].options["mode"], "shared")

    def test_controller_service_surface_is_capped_at_five_operations(self) -> None:
        fields = [field.name for field in dataclasses.fields(ControllerServices)]
        self.assertEqual(
            fields,
            [
                "run_trusted_agent",
                "materialize_variant",
                "evaluate_public_variant",
                "select_by_validation",
                "register_finalist",
            ],
        )

    def test_custom_step_and_policy_require_no_engine_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            base = load_config(minimal_config()).with_step(InspectStep(), after="static_audit")
            workflow = dataclasses.replace(
                base.workflow,
                after_iteration=(ComponentConfig.from_component(StopAfterFirstIteration()),),
            )
            config = dataclasses.replace(
                base,
                evolution=dataclasses.replace(
                    base.evolution,
                    iterations=3,
                    attempts_per_island=2,
                ),
                workflow=workflow,
            )
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=config,
                run_dir=root / "run",
                agent_runner=agent_runner,
                evaluator_factory=evaluator_factory,
            )
            result = engine.run()

            self.assertTrue(result.best_solver.is_dir())
            attempts = [
                json.loads(line)
                for line in (root / "run" / "attempts.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(attempts), 4)
            self.assertEqual({item["iteration"] for item in attempts}, {1})
            self.assertFalse({"population", "validation", "hidden", "config"} & InspectStep.seen)
            self.assertTrue((root / "run" / "status.html").is_file())
            self.assertTrue((root / "run" / "report.html").is_file())
            self.assertTrue((root / "run" / "public_events.jsonl").is_file())
            self.assertTrue((root / "run" / "public_run_state.json").is_file())

    def test_provenance_and_events_share_attempt_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            config = load_config(minimal_config()).with_step(
                ComponentConfig(
                    "nested_options",
                    {"tools": {"network": True, "web_search": False}},
                    _factory=NestedOptionsStep,
                ),
                after="static_audit",
            )
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=config,
                run_dir=root / "run",
                agent_runner=agent_runner,
                evaluator_factory=evaluator_factory,
            )
            engine.run()
            events = [
                json.loads(line)
                for line in (root / "run" / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
            attempt_ids = {
                event["scope"]["attempt_id"] for event in events if "attempt_id" in event["scope"]
            }
            recorded = {
                json.loads(line)["attempt_id"]
                for line in (root / "run" / "attempts.jsonl").read_text().splitlines()
            }
            self.assertEqual(attempt_ids, recorded)
            provenance = json.loads((root / "run" / "provenance.json").read_text())
            self.assertIn("config_hash", provenance)
            self.assertIn("source_hash", provenance["components"]["attempt_steps"][0])
            nested = next(
                step
                for step in provenance["components"]["attempt_steps"]
                if step["name"] == "nested_options"
            )
            self.assertEqual(
                nested["options"]["tools"],
                {"network": True, "web_search": False},
            )
            run_started = next(event for event in events if event["kind"] == "run_started")
            event_nested = next(
                step
                for step in run_started["data"]["components"]["attempt_steps"]
                if step["name"] == "nested_options"
            )
            self.assertEqual(
                event_nested["options"]["tools"],
                {"network": True, "web_search": False},
            )
            self.assertIsNotNone(provenance["components"]["worker"]["source_file_hash"])
            self.assertIsNotNone(provenance["components"]["evaluator"]["source_file_hash"])
            self.assertFalse(provenance["components"]["evaluator"]["declared_deterministic"])
            status = (root / "run" / "status.html").read_text(encoding="utf-8")
            self.assertNotIn("fetch(", status)
            self.assertIn('http-equiv="refresh"', status)
            self.assertIn("mutate:succeeded", status)
            report = (root / "run" / "report.html").read_text(encoding="utf-8")
            self.assertIn("public_events.jsonl", report)
            self.assertNotIn('href="events.jsonl"', report)
            public_events = (root / "run" / "public_events.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("validation_score", public_events)
            self.assertNotIn("source_file_hash", public_events)

    def test_engine_safety_gate_survives_static_audit_ablation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            (source / "locked.txt").write_text("original", encoding="utf-8")
            config = load_config(minimal_config()).without_step("static_audit")
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=config,
                run_dir=root / "run",
                agent_runner=out_of_scope_runner,
                evaluator_factory=evaluator_factory,
            )
            result = engine.run()

            attempts = [
                json.loads(line)
                for line in (root / "run" / "attempts.jsonl").read_text().splitlines()
            ]
            self.assertTrue(all(not attempt["valid"] for attempt in attempts))
            self.assertEqual(result.best_candidate_id, "seed")

    def test_coordinate_seed_does_not_depend_on_optional_pipeline_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            captured: list[dict[str, int]] = []
            for label, remove_smoke in (("full", False), ("no-smoke", True)):
                capture = SeedCaptureStep()
                config = load_config(minimal_config()).with_step(
                    capture,
                    after="public_evaluate",
                )
                if remove_smoke:
                    config = config.without_step("smoke")
                EvolutionEngine(
                    initial=source,
                    interface=InterfaceSpec.parse("solver.py:solver"),
                    runtime="python",
                    editable=(".",),
                    config=config,
                    run_dir=root / label,
                    agent_runner=agent_runner,
                    evaluator_factory=evaluator_factory,
                ).run()
                captured.append(capture.seeds)
            self.assertEqual(captured[0], captured[1])

    def test_nonempty_budget_edit_fails_instead_of_silently_doing_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            base = load_config(minimal_config())
            config = dataclasses.replace(
                base,
                workflow=dataclasses.replace(
                    base.workflow,
                    after_iteration=(ComponentConfig.from_component(UnsupportedBudgetPolicy()),),
                ),
            )
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=config,
                run_dir=root / "run",
                agent_runner=agent_runner,
                evaluator_factory=evaluator_factory,
            )
            with self.assertRaisesRegex(NotImplementedError, "future scheduler"):
                engine.run()

    def test_worker_memory_is_a_public_field_allowlist(self) -> None:
        base = load_config(minimal_config())
        config = dataclasses.replace(
            base,
            workers=dataclasses.replace(
                base.workers,
                tools=dataclasses.replace(base.workers.tools, communication="global"),
            ),
        )
        engine = EvolutionEngine(
            initial=Path("solver"),
            interface=InterfaceSpec.parse("solver.py:solver"),
            runtime="python",
            editable=(".",),
            config=config,
            run_dir=Path("run"),
            agent_runner=agent_runner,
            evaluator_factory=evaluator_factory,
        )
        engine.attempt_history = [
            {
                "attempt_id": "it001-i00-a00",
                "candidate_id": "it001-i00-a00",
                "island": 0,
                "iteration": 1,
                "attempt_index": 0,
                "parent_id": "seed",
                "public_score": 0.7,
                "validation_score": 0.0,
                "valid": False,
                "error": "controller validation evaluation failed: SECRET",
                "changed_files": ["solver.py"],
                "worker_returncode": 0,
                "worker_timed_out": False,
            }
        ]
        memory = engine._memory_for(0)
        self.assertIsNotNone(memory)
        self.assertIn("public_score", memory)
        self.assertNotIn("validation", memory)
        self.assertNotIn("SECRET", memory)
        self.assertNotIn('"valid"', memory)

    def test_component_class_is_instantiated_not_returned_as_an_instance(self) -> None:
        component = build("step", ComponentConfig.from_component(InspectStep))
        self.assertIsInstance(component, InspectStep)

    def test_migration_targets_real_island_indexes_when_one_is_empty(self) -> None:
        records = tuple(
            CandidateRecord(
                candidate_id=f"c{island}",
                island=island,
                iteration=1,
                attempt_index=0,
                parent_id="seed",
                path=Path(f"c{island}"),
                tree_hash=str(island),
                public_score=0.5,
                validation_score=0.5,
            )
            for island in (0, 2)
        )
        view = IterationView(
            iteration=2,
            populations=((records[0],), (), (records[1],)),
            attempt_ids=(),
            rng_seed=0,
        )
        edit = MigrationPolicy(interval=2).propose(view)
        self.assertEqual({target for _candidate, target in edit.migrate}, {0, 2})


if __name__ == "__main__":
    unittest.main()
