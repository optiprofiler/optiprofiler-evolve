from __future__ import annotations

import json
import os
import subprocess
import tempfile
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


class BrokerTests(unittest.TestCase):
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
            self.assertNotIn("problems", payload)
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
