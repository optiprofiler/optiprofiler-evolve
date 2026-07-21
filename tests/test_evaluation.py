from __future__ import annotations

import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.config import EvaluationConfig
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.evaluation import (
    DockerOptiProfilerEvaluator,
    PythonOptiProfilerEvaluator,
    _problems_for_mode,
    _sanitize_worker_artifacts,
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
                backend="unsafe_local",
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
    def test_nested_frozen_config_serializes_at_docker_request_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            reference = root / "reference"
            candidate.mkdir()
            reference.mkdir()
            data = DataPlan(
                library="s2mpj",
                selection={},
                universe=("PUBLIC",),
                public=("PUBLIC",),
                validation=(),
                hidden=(),
                smoke=("PUBLIC",),
                split_seed=0,
                manifest_hash="test",
                aliases={"PUBLIC": "P_PUBLIC"},
            )
            evaluator = DockerOptiProfilerEvaluator(
                reference=reference,
                interface=InterfaceSpec.parse("solver.py:solver"),
                data=data,
                config=EvaluationConfig(
                    backend="docker",
                    docker_image="evaluator:test",
                    benchmark={"max_eval_factor": 200, "nested": {"n_jobs": 4}},
                ),
            )
            completed = types.SimpleNamespace(returncode=1, stdout="expected test failure")
            with patch("optiprofiler_evolve.evaluation.subprocess.run", return_value=completed):
                result = evaluator.evaluate(candidate, "public", root / "output")
            self.assertFalse(result.success)
            self.assertIn("code 1", result.error or "")

    def test_worker_artifacts_redact_text_and_withhold_matching_binary(self) -> None:
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
            leaked = root / "SECRET_NAME" / "details.txt"
            leaked.parent.mkdir()
            leaked.write_text("problem=SECRET_NAME", encoding="utf-8")
            binary = root / "plot.bin"
            binary.write_bytes(b"\xffSECRET_NAME\x00")
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("SECRET_NAME", encoding="utf-8")
            link = root / "linked.txt"
            link.symlink_to(outside)

            _sanitize_worker_artifacts(root, data)

            redacted = root / "P_OPAQUE" / "details.txt"
            self.assertTrue(redacted.is_file())
            self.assertEqual(redacted.read_text(encoding="utf-8"), "problem=P_OPAQUE")
            self.assertFalse(binary.exists())
            self.assertFalse(link.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "SECRET_NAME")
            report = json.loads((root / "redaction_report.json").read_text())
            self.assertEqual(report["removed_binary_artifacts"], 1)
            outside.unlink()

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
            for variable in (
                "OMP_NUM_THREADS=1",
                "OPENBLAS_NUM_THREADS=1",
                "MKL_NUM_THREADS=1",
                "NUMEXPR_NUM_THREADS=1",
            ):
                self.assertIn(variable, joined)
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
        self.assertEqual(_problems_for_mode(data, "public_score"), ("PUBLIC",))


if __name__ == "__main__":
    unittest.main()
