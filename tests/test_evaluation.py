from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.config import EvaluationConfig
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.evaluation import (
    DockerOptiProfilerEvaluator,
    PythonOptiProfilerEvaluator,
    _scoped_data_manifest,
)
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
                validation=(),
                hidden=(),
                smoke=("ROSENBR",),
                split_seed=0,
                manifest_hash="test",
                aliases={"ROSENBR": "P_OPAQUE"},
            )
            config = EvaluationConfig(
                backend="local",
                feedback_mode="agent",
                benchmark={"score_only": False, "n_jobs": 1, "max_eval_factor": 5},
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
            serialized = (root / "output" / "result.json").read_text(encoding="utf-8")
            self.assertNotIn("ROSENBR", serialized)
            self.assertTrue((root / "output" / "artifact_index.json").is_file())
            for path in (root / "output").rglob("*"):
                self.assertNotIn("ROSENBR", path.name)
                if path.suffix in {".json", ".log", ".md", ".txt"}:
                    self.assertNotIn("ROSENBR", path.read_text(encoding="utf-8"))


class DockerEvaluationBoundaryTests(unittest.TestCase):
    def test_trusted_request_is_mounted_outside_worker_visible_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = DataPlan(
                library="s2mpj",
                selection={},
                universe=("SECRET_NAME",),
                public=("SECRET_NAME",),
                validation=(),
                hidden=(),
                smoke=("SECRET_NAME",),
                split_seed=0,
                manifest_hash="test",
                aliases={"SECRET_NAME": "P_OPAQUE"},
            )
            evaluator = DockerOptiProfilerEvaluator(
                reference=root / "reference",
                interface=InterfaceSpec.parse("solver.py:solver"),
                data=data,
                config=EvaluationConfig(backend="docker", docker_image="evaluator:test"),
            )
            request = root / "controller-only" / "evaluation_request.json"
            request.parent.mkdir()
            request.write_text("{}", encoding="utf-8")
            output = root / "worker-visible-artifacts"
            output.mkdir()
            command = evaluator.command(root / "candidate", output, "test-container", request)
            joined = " ".join(command)
            self.assertIn(f"src={request.parent},dst=/request", joined)
            self.assertIn("/request/evaluation_request.json", joined)
            self.assertIn("/pycutest-cache:rw,exec,nosuid,nodev,size=2g", joined)
            self.assertIn("PYCUTEST_CACHE=/pycutest-cache", joined)
            self.assertNotIn(str(output / "evaluation_request.json"), joined)
            self.assertNotIn("SECRET_NAME", joined)

    def test_public_request_excludes_validation_and_hidden_names(self) -> None:
        data = DataPlan(
            library="s2mpj",
            selection={"ptype": "u"},
            universe=("PUBLIC", "VALIDATION", "HIDDEN"),
            public=("PUBLIC",),
            validation=("VALIDATION",),
            hidden=("HIDDEN",),
            smoke=("PUBLIC",),
            split_seed=0,
            manifest_hash="test",
            aliases={
                "PUBLIC": "P_PUBLIC",
                "VALIDATION": "P_VALIDATION",
                "HIDDEN": "P_HIDDEN",
            },
        )
        serialized = json.dumps(_scoped_data_manifest(data, "public"))
        self.assertIn("PUBLIC", serialized)
        self.assertNotIn("VALIDATION", serialized)
        self.assertNotIn("HIDDEN", serialized)


if __name__ == "__main__":
    unittest.main()
