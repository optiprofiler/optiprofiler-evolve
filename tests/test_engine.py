from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path

from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.engine import EvolutionEngine, _controller_cancellation
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.sandbox import AgentRunResult
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash

from test_config import minimal_config


class FakeEvaluator:
    calls: list[str] = []

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        self.calls.append(mode)
        output_dir.mkdir(parents=True, exist_ok=True)
        improved = "improved" in (candidate / "solver.py").read_text()
        score = 0.7 if improved else 0.5
        if mode == "validation" and improved:
            score = 0.68
        if mode == "hidden" and improved:
            score = 0.65
        result = EvaluationResult(mode, score, score, 0.5, 1, output_dir)
        (output_dir / "result.json").write_text(json.dumps(result.as_dict(), default=str))
        (output_dir / "feedback.md").write_text("ok")
        return result


def fake_evaluator_factory(**_kwargs) -> FakeEvaluator:
    return FakeEvaluator()


def fake_agent_runner(**kwargs) -> AgentRunResult:
    workspace = kwargs["workspace"]
    transcript = kwargs["transcript"]
    solver = workspace / "solver.py"
    solver.write_text(solver.read_text() + "\n# improved\n")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("fake worker\n")
    return AgentRunResult(0, transcript)


class EngineTests(unittest.TestCase):
    def test_population_loop_preserves_source_and_tests_one_validated_champion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            original_hash = tree_hash(source)
            config = load_config(minimal_config())
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=config,
                run_dir=root / "run",
                agent_runner=fake_agent_runner,
                evaluator_factory=fake_evaluator_factory,
            )
            result = engine.run()
            self.assertEqual(tree_hash(source), original_hash)
            self.assertEqual(result.public_score, 0.7)
            self.assertEqual(result.validation_score, 0.68)
            self.assertEqual(result.final_score, 0.65)
            self.assertEqual(FakeEvaluator.calls.count("public_score"), 3)
            self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)
            self.assertIn("improved", (result.best_solver / "solver.py").read_text())
            first_workspace = result.run_dir / "workspaces" / "it001-i00-a00"
            second_workspace = result.run_dir / "workspaces" / "it001-i01-a00"
            self.assertTrue(first_workspace.is_dir())
            self.assertTrue(second_workspace.is_dir())
            self.assertNotEqual(first_workspace.resolve(), second_workspace.resolve())
            self.assertNotEqual(first_workspace.resolve(), source.resolve())
            public_manifest = (result.run_dir / "public_data_manifest.json").read_text()
            self.assertNotIn("controller/data_manifest", public_manifest)
            self.assertNotIn("P1", public_manifest)
            self.assertTrue((result.run_dir / "checkpoints" / "iteration_001.json").is_file())
            for trace_root in sorted((result.run_dir / "traces").iterdir()):
                self.assertEqual((trace_root / "raw.stdout.stream").read_text(), "fake worker\n")
                terminal = json.loads((trace_root / "outcome.json").read_text())
                self.assertEqual(terminal["state"], "completed")
                self.assertIn("without native chunk timing", terminal["capture_error"])

    @unittest.skipUnless(os.name == "posix", "signal test requires POSIX")
    def test_controller_signal_scope_requests_cooperative_cancellation(self) -> None:
        event = threading.Event()
        with _controller_cancellation(event):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(event.wait(timeout=1))


if __name__ == "__main__":
    unittest.main()
