from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from optiprofiler_evolve.agents.broker import EvaluationBroker
from optiprofiler_evolve.models import EvaluationResult


class FakeEvaluator:
    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        result = EvaluationResult(mode, 0.6, 0.7, 0.5, 1, output_dir)
        (output_dir / "result.json").write_text(json.dumps(result.as_dict(), default=str))
        (output_dir / "feedback.md").write_text("ok")
        return result


class BlockingEvaluator:
    def __init__(self, ready: threading.Event, release: threading.Event) -> None:
        self.ready = ready
        self.release = release

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "evaluator.log").write_text("not yet public", encoding="utf-8")
        self.ready.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test evaluator was not released")
        result = EvaluationResult(mode, 0.6, 0.7, 0.5, 1, output_dir)
        (output_dir / "result.json").write_text(json.dumps(result.as_dict()), encoding="utf-8")
        (output_dir / "feedback.md").write_text("ok", encoding="utf-8")
        return result


class BrokerTests(unittest.TestCase):
    def test_bad_reports_keep_the_worker_score_and_charge_only_one_call(self):
        for location, contents in (
            ("", "broken report"),
            ("benchmark", "broken report"),
            ("benchmark", '{"schema":"optiprofiler.eval_report/2","plot_data":{"path":"secret.json"}}'),
            ("benchmark", '{"schema":"optiprofiler.eval_report/1","plot_data":{"path":"../secret.json"}}'),
        ):
            with self.subTest(location=location, contents=contents), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                workspace.mkdir()
                emit = Mock()

                class ReportEvaluator(FakeEvaluator):
                    calls = 0

                    def evaluate(self, candidate, mode, output_dir):
                        self.calls += 1
                        result = super().evaluate(candidate, mode, output_dir)
                        (output_dir / "benchmark").mkdir()
                        (output_dir / location / "eval_report.json").write_text(contents)
                        (output_dir / "benchmark/secret.json").write_text("PRIVATE_NUMBERS")
                        return result

                evaluator = ReportEvaluator()
                with EvaluationBroker(
                    workspace=workspace, control_dir=root / "controller", evaluator=evaluator,
                    max_smoke_calls=1, max_public_calls=0, emit=emit,
                ) as broker:
                    tools = workspace / "tools"
                    broker.install_tools(tools)
                    connection = broker.start(docker=True)
                    environment = {
                        **os.environ,
                        "OPTIPROFILER_EVOLVE_BROKER_DIR": str(connection.host_directory),
                        "OPTIPROFILER_EVOLVE_BROKER_TOKEN": connection.token,
                        "OPTIPROFILER_EVOLVE_WORKSPACE": str(workspace),
                    }
                    completed = subprocess.run(
                        [str(tools / "smoke_test")], env=environment, capture_output=True,
                        text=True, timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    response = json.loads(completed.stdout)
                    self.assertTrue(response["success"])
                    self.assertEqual(response["score"], 0.6)
                    self.assertNotIn(str(root), completed.stdout)
                    self.assertNotIn("PRIVATE_NUMBERS", completed.stdout)
                    self.assertEqual(broker.counts["smoke"], 1)
                    self.assertEqual(evaluator.calls, 1)
                    public = broker.artifacts / "evaluations/smoke/001"
                    self.assertFalse((public / "benchmark").exists())
                    self.assertNotIn(str(root), (public / "feedback.md").read_text())
                    private = broker.control_dir / "private_evaluations/smoke/001"
                    self.assertTrue((private / "benchmark/secret.json").exists())
                    diagnostic = json.loads((private / "feedback_warning.json").read_text())
                    emit.assert_called_once_with("feedback_artifacts_withheld", "failed", diagnostic)
                    exhausted = subprocess.run(
                        [str(tools / "smoke_test")], env=environment, capture_output=True,
                        text=True, timeout=10,
                    )
                    self.assertNotEqual(exhausted.returncode, 0)
                    self.assertEqual(evaluator.calls, 1)

    def test_private_exception_text_is_never_in_worker_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            broker = EvaluationBroker(
                workspace=workspace, control_dir=root / "controller", evaluator=FakeEvaluator(),
                candidate_validator=Mock(side_effect=FileExistsError(f"PRIVATE_PATH {root}/secret")),
                max_smoke_calls=1, max_public_calls=0,
            )
            with broker:
                broker.start(docker=True)
                request_id = uuid.uuid4().hex
                request = broker.exchange / "requests" / f"{request_id}.json"
                # Process synchronously to inspect both owner diagnostics and the actual response.
                broker.stop()
                request.write_text(json.dumps({"id": request_id, "token": broker.token, "mode": "smoke"}))
                with self.assertLogs("optiprofiler_evolve.agents.broker", level="ERROR") as logs:
                    broker._process(request)
                response = json.loads((broker.exchange / "responses" / f"{request_id}.json").read_text())
                self.assertEqual(response["status"], 500)
                self.assertNotIn(str(root), json.dumps(response))
                self.assertNotIn("PRIVATE_PATH", json.dumps(response))
                self.assertIn("PRIVATE_PATH", "".join(logs.output))

    def test_private_evidence_is_projected_by_feedback_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()

            class EvidenceEvaluator(FakeEvaluator):
                def evaluate(
                    self,
                    candidate: Path,
                    mode: str,
                    output_dir: Path,
                ) -> EvaluationResult:
                    result = super().evaluate(candidate, mode, output_dir)
                    benchmark = output_dir / "benchmark"
                    benchmark.mkdir()
                    (benchmark / "profile.png").write_bytes(b"png")
                    (benchmark / "run.log").write_text("log", encoding="utf-8")
                    (benchmark / "state.pkl").write_bytes(b"private")
                    (output_dir / "private_notes.txt").write_text(
                        "controller only",
                        encoding="utf-8",
                    )
                    return result

            for mode in ("score_only", "full_artifacts"):
                control = root / mode
                broker = EvaluationBroker(
                    workspace=workspace,
                    control_dir=control,
                    evaluator=EvidenceEvaluator(),
                    max_smoke_calls=1,
                    max_public_calls=1,
                    feedback_mode=mode,
                )
                published = control / "artifacts/evaluations/public/001"
                result = broker._evaluate_and_publish("public", published)

                self.assertEqual(result.output_dir, published)
                private = control / "private_evaluations/public/001"
                self.assertTrue((private / "benchmark/state.pkl").is_file())
                worker_result = json.loads((published / "result.json").read_text())
                self.assertNotIn("candidate_score", worker_result)
                self.assertNotIn("reference_score", worker_result)
                self.assertFalse((published / "private_notes.txt").exists())
                self.assertFalse((published / "benchmark/state.pkl").exists())

                if mode == "score_only":
                    files = sorted(
                        path.relative_to(published).as_posix()
                        for path in published.rglob("*")
                    )
                    self.assertEqual(files, ["feedback.md", "result.json"])
                else:
                    self.assertTrue((published / "benchmark/profile.png").is_file())
                    self.assertTrue((published / "benchmark/run.log").is_file())
                    self.assertTrue((published / "artifact_index.json").is_file())

    def test_evaluation_tree_is_published_only_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            ready = threading.Event()
            release = threading.Event()
            broker = EvaluationBroker(
                workspace=workspace,
                control_dir=root / "broker-control",
                evaluator=BlockingEvaluator(ready, release),
                max_smoke_calls=1,
                max_public_calls=0,
            )
            connection = broker.start(docker=False)
            request_id = uuid.uuid4().hex
            request = broker.exchange / "requests" / f"{request_id}.json"
            response = broker.exchange / "responses" / f"{request_id}.json"
            temporary = request.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"id": request_id, "token": connection.token, "mode": "smoke"}),
                encoding="utf-8",
            )
            temporary.replace(request)
            self.assertTrue(ready.wait(timeout=2))
            published = broker.artifacts / "evaluations" / "smoke" / "001"
            self.assertFalse(published.exists())
            self.assertTrue(any((broker.control_dir / "staging").iterdir()))
            release.set()
            for _ in range(200):
                if response.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(response.exists())
            self.assertTrue((published / "evaluator.log").is_file())
            self.assertFalse(any((broker.control_dir / "staging").iterdir()))
            broker.stop()

    def test_public_capabilities_work_and_hidden_is_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            broker = EvaluationBroker(
                workspace=workspace,
                control_dir=root / "broker-control",
                evaluator=FakeEvaluator(),
                max_smoke_calls=1,
                max_public_calls=1,
            )
            tools = workspace / "tools"
            broker.install_tools(tools)
            connection = broker.start(docker=False)
            environment = dict(os.environ)
            environment.update(
                {
                    "OPTIPROFILER_EVOLVE_BROKER_DIR": connection.directory,
                    "OPTIPROFILER_EVOLVE_BROKER_TOKEN": connection.token,
                    "OPTIPROFILER_EVOLVE_WORKSPACE": str(workspace),
                }
            )
            completed = subprocess.run(
                [str(tools / "smoke_test")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                check=False,
                timeout=10,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["problem_count"], 1)
            self.assertEqual(payload["direction"], "maximize")
            self.assertNotIn("problems", payload)
            self.assertNotIn("candidate_score", payload)
            self.assertNotIn("reference_score", payload)
            worker_result = json.loads(
                (workspace / ".optiprofiler_evolve/evaluations/smoke/001/result.json").read_text()
            )
            self.assertNotIn("candidate_score", worker_result)
            self.assertNotIn("reference_score", worker_result)
            mounted_result = json.loads(
                (broker.artifacts / "evaluations/smoke/001/result.json").read_text()
            )
            self.assertNotIn("candidate_score", mounted_result)
            self.assertNotIn("reference_score", mounted_result)
            exhausted = subprocess.run(
                [str(tools / "smoke_test")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                check=False,
                timeout=10,
            )
            exhausted_payload = json.loads(exhausted.stdout)
            self.assertNotEqual(exhausted.returncode, 0)
            self.assertEqual(exhausted_payload["error"], "evaluation quota exhausted")
            self.assertFalse(exhausted_payload["retryable"])
            self.assertEqual(exhausted_payload["remaining"], 0)
            validation_id = uuid.uuid4().hex
            validation_request = broker.exchange / "requests" / f"{validation_id}.json"
            validation_response = broker.exchange / "responses" / f"{validation_id}.json"
            validation_request.write_text(
                json.dumps(
                    {"id": validation_id, "token": connection.token, "mode": "validation"}
                )
            )
            for _ in range(100):
                if validation_response.exists():
                    break
                time.sleep(0.01)
            self.assertEqual(json.loads(validation_response.read_text())["status"], 403)
            request_id = uuid.uuid4().hex
            request = broker.exchange / "requests" / f"{request_id}.json"
            victim = root / "victim.txt"
            victim.write_text("unchanged", encoding="utf-8")
            response = broker.exchange / "responses" / f"{request_id}.json"
            response.symlink_to(victim)
            request.write_text(
                json.dumps({"id": request_id, "token": connection.token, "mode": "hidden"})
            )
            for _ in range(100):
                if response.exists() and not response.is_symlink():
                    break
                time.sleep(0.01)
            self.assertEqual(json.loads(response.read_text())["status"], 403)
            self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged")
            broker.stop()


if __name__ == "__main__":
    unittest.main()
