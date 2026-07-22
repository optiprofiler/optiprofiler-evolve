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

from optiprofiler_evolve.broker import EvaluationBroker
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
        (output_dir / "partial.txt").write_text("not yet public", encoding="utf-8")
        self.ready.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test evaluator was not released")
        result = EvaluationResult(mode, 0.6, 0.7, 0.5, 1, output_dir)
        (output_dir / "result.json").write_text(json.dumps(result.as_dict()), encoding="utf-8")
        (output_dir / "feedback.md").write_text("ok", encoding="utf-8")
        return result


class BrokerTests(unittest.TestCase):
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
            request.write_text(
                json.dumps({"id": request_id, "token": connection.token, "mode": "smoke"}),
                encoding="utf-8",
            )
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
            self.assertTrue((published / "partial.txt").is_file())
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
