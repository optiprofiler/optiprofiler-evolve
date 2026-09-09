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

from optiprofiler_evolve.controller.builtins import register_builtin_components
from optiprofiler_evolve.controller.engine import EvolutionEngine
from optiprofiler_evolve.controller.phase_contexts import bind_context, commit_result
from optiprofiler_evolve.controller.registry import resolve
from optiprofiler_evolve.phases.prepare.phase import PreparePhase
from optiprofiler_evolve.shared.config import load_config
from optiprofiler_evolve.shared.protocols import ControllerServices, PhaseContext
from optiprofiler_evolve.shared.solver import InterfaceSpec


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
                self.assertEqual(type(phase).__module__, f"optiprofiler_evolve.phases.{name}.phase")
                self.assertIn("run", type(phase).__dict__)
                self.assertNotIn(".invoke(", inspect.getsource(type(phase)))
        self.assertNotIn("invoke", {field.name for field in fields(PhaseContext)})
        self.assertEqual(len(fields(ControllerServices)), 5)

    def test_preparation_returns_state_without_committing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initial = root / "solver.py"
            initial.write_text("def solver(fun, x0):\n    return x0\n")
            engine = EvolutionEngine(
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
            context = bind_context(engine, "prepare", base)
            self.assertFalse(hasattr(context, "engine"))
            result = PreparePhase().run(context)
            self.assertIsNone(engine.data)
            self.assertEqual(engine.populations, [])
            self.assertEqual(engine.candidates, {})
            commit_result(engine, result)
            self.assertIs(engine.data, result.data)
            self.assertEqual(engine.candidates, {"seed": result.seed})
            self.assertTrue(all(population == [result.seed] for population in engine.populations))
            self.assertEqual(initial.read_text(), "def solver(fun, x0):\n    return x0\n")

    def test_worker_backend_has_one_invocation_entrypoint(self) -> None:
        package = Path(inspect.getfile(EvolutionEngine)).parents[1]
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
        for removed in ("engine.py", "phases/builtin.py", "phases/research.py"):
            self.assertFalse((package / removed).exists())
