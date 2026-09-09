from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from test_config import minimal_config
from test_extensibility import agent_runner, evaluator_factory
from worker_helpers import RunnerAdapter

from optiprofiler_evolve.application.run import EvolutionRun
from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.controller.builtins import register_builtin_components
from optiprofiler_evolve.controller.contracts import ControllerServices
from optiprofiler_evolve.controller.registry import resolve
from optiprofiler_evolve.phases.contracts import PhaseContext
from optiprofiler_evolve.phases.prepare import PreparePhase
from optiprofiler_evolve.solver import InterfaceSpec


class PhaseLayoutTests(unittest.TestCase):
    def test_every_stage_has_its_own_class_and_real_run_method(self) -> None:
        register_builtin_components()
        for name in (
            "prepare",
            "explore",
            "validate",
            "hidden",
            "report",
            "direction_scout",
            "strategy_analysis",
            "strategy_transfer",
            "recombine",
            "challenger",
        ):
            with self.subTest(phase=name):
                phase = resolve("phase", name)()
                self.assertEqual(phase.name, name)
                self.assertEqual(type(phase).__module__, f"optiprofiler_evolve.phases.{name}")
                self.assertEqual(Path(inspect.getfile(type(phase))).name, f"{name}.py")
                self.assertIn("run", type(phase).__dict__)
                self.assertNotIn(".invoke(", inspect.getsource(type(phase)))
        self.assertNotIn("invoke", {field.name for field in fields(PhaseContext)})
        self.assertEqual(len(fields(ControllerServices)), 5)

    def test_preparation_returns_state_without_committing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initial = root / "solver.py"
            initial.write_text("def solver(fun, x0):\n    return x0\n")
            engine = EvolutionRun(
                initial=initial,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("*.py",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(agent_runner),
                evaluator_factory=evaluator_factory,
            )
            engine._initialize_run_directory()
            base = PhaseContext(
                run_dir=engine.run_dir,
                options={},
                artifacts={},
                emit=lambda *args: None,
                services=engine.controller_services,
            )
            context = engine.coordinator.contexts["prepare"](base)
            self.assertFalse(hasattr(context, "engine"))
            result = PreparePhase().run(context)
            self.assertIsNone(engine.state.data)
            self.assertEqual(engine.state.populations, [])
            self.assertEqual(engine.state.candidates, {})
            engine.commits.commit_phase_result(result)
            self.assertIs(engine.state.data, result.data)
            self.assertEqual(engine.state.candidates, {"seed": result.seed})
            self.assertTrue(all(population == [result.seed] for population in engine.state.populations))
            self.assertEqual(initial.read_text(), "def solver(fun, x0):\n    return x0\n")

    def test_worker_backend_has_one_invocation_entrypoint(self) -> None:
        package = Path(inspect.getfile(EvolutionRun)).parents[1]
        sites = []
        for path in package.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                owner = node.func.value
                if node.func.attr == "run" and isinstance(owner, ast.Attribute):
                    if owner.attr in {"adapter", "worker_adapter"}:
                        sites.append(path.relative_to(package).as_posix())
        self.assertEqual(sites, ["agents/llm.py"])
        for removed in ("engine.py", "phases/builtin.py", "phases/research.py",
                        "quality", "shared", "evaluation"):
            self.assertFalse((package / removed).exists())

    def test_phase_contexts_live_with_their_stage(self) -> None:
        import importlib

        for name in ("prepare", "explore", "validate", "hidden", "report"):
            module = importlib.import_module(f"optiprofiler_evolve.phases.{name}")
            context = getattr(module, f"{name.capitalize()}Context")
            self.assertEqual(context.__module__, module.__name__)

    def test_contracts_are_owned_by_their_subsystems(self) -> None:
        from optiprofiler_evolve.agents.contracts import AgentJob, WorkerRequest
        from optiprofiler_evolve.criteria.performance.contracts import Evaluator
        from optiprofiler_evolve.phases.attempts.contracts import AttemptContext

        for contract, module in (
            (AgentJob, "agents.contracts"), (WorkerRequest, "agents.contracts"),
            (Evaluator, "criteria.performance.contracts"),
            (AttemptContext, "phases.attempts.contracts"),
            (PhaseContext, "phases.contracts"), (ControllerServices, "controller.contracts"),
        ):
            with self.subTest(contract=contract.__name__):
                self.assertEqual(contract.__module__, f"optiprofiler_evolve.{module}")
