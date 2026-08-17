from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.broker import EvaluationBroker
from optiprofiler_evolve.config import EvaluationConfig
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.evaluation import (
    DockerOptiProfilerEvaluator,
    PythonOptiProfilerEvaluator,
    _profile_scores_mean_fitness,
    _problems_for_mode,
    _sanitize_worker_artifacts,
    _scoped_data_manifest,
)
from optiprofiler_evolve.solver import InterfaceSpec


class ProfileTensorFitnessTests(unittest.TestCase):
    def test_tie_maps_every_tensor_entry_to_one_half(self) -> None:
        profile_scores = [
            [[[0.2, 0.4, 0.8], [0.1, 0.3, 0.7]]],
            [[[0.2, 0.4, 0.8], [0.1, 0.3, 0.7]]],
        ]

        fitness, normalized = _profile_scores_mean_fitness(profile_scores)

        self.assertEqual(fitness, 0.5)
        self.assertTrue((normalized == 0.5).all())

    def test_mean_uses_every_profile_entry(self) -> None:
        profile_scores = [
            [[[1.0, 0.8, 0.6], [0.4, 0.2, 0.0]]],
            [[[0.0, 0.2, 0.4], [0.6, 0.8, 1.0]]],
        ]

        fitness, normalized = _profile_scores_mean_fitness(profile_scores)

        expected = [1.0, 0.8, 0.6, 0.4, 0.2, 0.0]
        self.assertAlmostEqual(fitness, sum(expected) / len(expected))
        for actual, target in zip(normalized.flatten().tolist(), expected, strict=True):
            self.assertAlmostEqual(actual, target)

    def test_requires_paired_four_dimensional_tensor(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "shape"):
            _profile_scores_mean_fitness([[1.0], [0.0]])


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
                fitness_source="profile_scores_mean",
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
            breakdown = json.loads(
                (root / "output" / "fitness_breakdown.json").read_text(encoding="utf-8")
            )
            self.assertEqual(breakdown["fitness_source"], "profile_scores_mean")
            self.assertEqual(breakdown["fitness"], 0.5)
            self.assertTrue(breakdown["normalized_profile_advantage"])
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
            internal_source = root / "benchmark" / "test_log" / "profiles.py"
            internal_source.parent.mkdir(parents=True)
            internal_source.write_text("def benchmark_internal(): pass\n", encoding="utf-8")
            raw_state = internal_source.with_name("curves.pkl")
            raw_state.write_bytes(b"pickle-state")
            useful_log = internal_source.with_name("report.txt")
            useful_log.write_text("public summary", encoding="utf-8")
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
            self.assertFalse(internal_source.exists())
            self.assertFalse(raw_state.exists())
            self.assertEqual(useful_log.read_text(encoding="utf-8"), "public summary")
            self.assertEqual(outside.read_text(encoding="utf-8"), "SECRET_NAME")
            report = json.loads((root / "redaction_report.json").read_text())
            self.assertEqual(report["removed_binary_artifacts"], 1)
            self.assertEqual(report["removed_internal_artifacts"], 2)
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


class DockerRequestStagingTests(unittest.TestCase):
    """The /request bind source must be run-owned, short-lived, and private."""

    def _make_evaluator(self, root: Path) -> DockerOptiProfilerEvaluator:
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
        reference = root / "reference"
        reference.mkdir(exist_ok=True)
        return DockerOptiProfilerEvaluator(
            reference=reference,
            interface=InterfaceSpec.parse("solver.py:solver"),
            data=data,
            config=EvaluationConfig(backend="docker", docker_image="evaluator:test"),
        )

    def _run_and_capture(self, evaluator, candidate, output_dir, *, timeout=False):
        seen = {}

        def fake_run(command, **kwargs):
            if list(command[:2]) == ["docker", "rm"]:
                return types.SimpleNamespace(returncode=0, stdout="")
            mount = next(item for item in command if ",dst=/request" in str(item))
            source = Path(str(mount).split("src=", 1)[1].split(",dst=", 1)[0])
            seen["source"] = source
            seen["existed_during_call"] = source.is_dir()
            seen["request_file_during_call"] = (
                source / "evaluation_request.json"
            ).is_file()
            if timeout:
                raise subprocess.TimeoutExpired(cmd=command, timeout=1, output=b"late")
            return types.SimpleNamespace(returncode=7, stdout="evaluator refused")

        with patch(
            "optiprofiler_evolve.evaluation.subprocess.run", side_effect=fake_run
        ):
            result = evaluator.evaluate(candidate, "smoke", output_dir)
        return seen, result

    def _assert_no_request_leftovers(self, root: Path) -> None:
        leftovers = [
            path
            for path in root.rglob(".ope-evaluator-request-*")
            if path.exists()
        ]
        self.assertEqual(leftovers, [])

    def test_bind_source_is_sibling_of_output_and_exists_during_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluator = self._make_evaluator(root)
            output_dir = root / "controller" / "evaluations" / "seed" / "public"
            seen, result = self._run_and_capture(
                evaluator, root / "candidate", output_dir
            )

            self.assertTrue(seen["existed_during_call"])
            self.assertTrue(seen["request_file_during_call"])
            self.assertEqual(seen["source"].parent, output_dir.resolve().parent)
            self.assertTrue(seen["source"].name.startswith(".ope-evaluator-request-"))
            self.assertFalse(result.success)
            self._assert_no_request_leftovers(root)

    def test_staging_is_cleaned_after_failure_and_timeout(self) -> None:
        for timeout in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evaluator = self._make_evaluator(root)
                output_dir = root / "controller" / "evaluations" / "cand" / "public"
                seen, result = self._run_and_capture(
                    evaluator, root / "candidate", output_dir, timeout=timeout
                )
                self.assertFalse(result.success)
                self.assertFalse(seen["source"].exists())
                self._assert_no_request_leftovers(root)

    def test_request_mount_writable_and_hardening_flags_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluator = self._make_evaluator(root)
            request = root / "controller-only" / "evaluation_request.json"
            request.parent.mkdir()
            request.write_text("{}", encoding="utf-8")
            output = root / "output"
            output.mkdir()
            command = evaluator.command(root / "candidate", output, "name", request)

            request_mount = next(item for item in command if ",dst=/request" in item)
            # Writable on purpose: the in-container runner unlinks the request
            # (real problem names) before candidate code can run.
            self.assertNotIn("readonly", request_mount)
            for destination in ("/candidate", "/reference"):
                mount = next(item for item in command if f",dst={destination}" in item)
                self.assertIn("readonly", mount)
            joined = " ".join(command)
            self.assertIn("--network none", joined)
            self.assertIn("--cap-drop ALL", joined)
            self.assertIn("--read-only", joined)
            self.assertIn("no-new-privileges", joined)

    def test_request_staging_never_enters_published_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluator = self._make_evaluator(root)
            workspace = root / "workspace"
            workspace.mkdir()
            control = root / "controller" / "brokers" / "cand"
            broker = EvaluationBroker(
                workspace=workspace,
                control_dir=control,
                evaluator=evaluator,
                max_smoke_calls=5,
                max_public_calls=5,
                candidate_validator=lambda candidate_root: None,
            )
            output = control / "artifacts" / "evaluations" / "smoke" / "000"

            seen = {}

            def fake_run(command, **kwargs):
                if list(command[:2]) == ["docker", "rm"]:
                    return types.SimpleNamespace(returncode=0, stdout="")
                mount = next(item for item in command if ",dst=/request" in str(item))
                source = Path(str(mount).split("src=", 1)[1].split(",dst=", 1)[0])
                seen["source_parent"] = source.parent
                return types.SimpleNamespace(returncode=7, stdout="refused")

            with patch(
                "optiprofiler_evolve.evaluation.subprocess.run", side_effect=fake_run
            ):
                broker._evaluate_and_publish("smoke", output)

            self.assertEqual(seen["source_parent"], (control / "staging").resolve())
            self.assertTrue(output.is_dir())
            self._assert_no_request_leftovers(control)
            published = {path.name for path in output.iterdir()}
            self.assertNotIn(
                True, [name.startswith(".ope-evaluator-request-") for name in published]
            )


if __name__ == "__main__":

    unittest.main()
