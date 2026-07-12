from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.engine import EvolutionEngine
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.sandbox import AgentRunResult
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash

from test_config import minimal_config


class FakeEvaluator:
    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        improved = "improved" in (candidate / "solver.py").read_text()
        score = 0.7 if improved else 0.5
        if mode == "final" and improved:
            score = 0.65
        result = EvaluationResult(mode, score, score, 0.5, ("P1",), output_dir)
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
    def test_population_loop_preserves_source_and_reranks_fixed_finalists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
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
            self.assertEqual(result.final_score, 0.65)
            self.assertIn("improved", (result.best_solver / "solver.py").read_text())
            public_manifest = (result.run_dir / "public_data_manifest.json").read_text()
            self.assertNotIn("controller/data_manifest", public_manifest)
            self.assertTrue((result.run_dir / "checkpoints" / "generation_001.json").is_file())


if __name__ == "__main__":
    unittest.main()
