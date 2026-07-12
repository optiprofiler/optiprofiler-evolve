from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.config import EvaluationConfig
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.evaluation import PythonOptiProfilerEvaluator
from optiprofiler_evolve.solver import InterfaceSpec


@unittest.skipIf(importlib.util.find_spec("optiprofiler") is None, "optiprofiler unavailable")
class OptiProfilerEvaluationTests(unittest.TestCase):
    def test_identical_multifile_solver_has_normalized_tie_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("candidate", "reference"):
                solver_root = root / name
                solver_root.mkdir()
                (solver_root / "helper.py").write_text(
                    "def unchanged(x):\n    return x\n", encoding="utf-8"
                )
                (solver_root / "solver.py").write_text(
                    "from .helper import unchanged\n\n"
                    "def solver(fun, x0):\n    return unchanged(x0)\n",
                    encoding="utf-8",
                )
            data = DataPlan(
                library="s2mpj",
                selection={},
                universe=("ROSENBR",),
                public=("ROSENBR",),
                hidden=(),
                smoke=("ROSENBR",),
                split_seed=0,
                manifest_hash="test",
            )
            config = EvaluationConfig(
                backend="local",
                benchmark={"score_only": True, "n_jobs": 1, "max_eval_factor": 5},
            )
            evaluator = PythonOptiProfilerEvaluator(
                reference=root / "reference",
                interface=InterfaceSpec.parse("solver.py:solver"),
                data=data,
                config=config,
            )
            result = evaluator.evaluate(root / "candidate", "public", root / "output")
            self.assertTrue(result.success, result.error)
            self.assertEqual(result.score, 0.5)
            self.assertEqual(result.candidate_score, result.reference_score)


if __name__ == "__main__":
    unittest.main()
