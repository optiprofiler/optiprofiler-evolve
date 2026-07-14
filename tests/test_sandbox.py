from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.broker import BrokerConnection
from optiprofiler_evolve.config import (
    SandboxConfig,
    ToolConfig,
    WorkerConfig,
    WorkersConfig,
)
from optiprofiler_evolve.sandbox import _docker_command, run_agent


class SandboxCommandTests(unittest.TestCase):
    def test_docker_boundary_is_hardened_and_secret_values_are_not_in_argv(self) -> None:
        worker = WorkerConfig(harness="codex", model="test", env={"OPENAI_API_KEY": "super-secret"})
        workers = WorkersConfig(pool=(worker,), tools=ToolConfig(network=False, web_search=False))
        command = _docker_command(
            workers=workers,
            sandbox=SandboxConfig(),
            workspace=Path("/workspace-host"),
            tools_dir=Path("/tools-host"),
            broker=BrokerConnection(
                "/opt/broker",
                "/opt/artifacts",
                "token",
                Path("/broker-host"),
                Path("/artifacts-host"),
            ),
            harness=["codex", "exec"],
            network="private-network",
            container_name="ope-worker-test",
            selected_values=worker.env,
        )
        joined = " ".join(command)
        self.assertIn("--interactive", command)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges", joined)
        self.assertIn("--read-only", joined)
        self.assertIn("--network private-network", joined)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("super-secret", joined)

    def test_each_networked_worker_gets_a_temporary_dedicated_network(self) -> None:
        worker = WorkerConfig(harness="codex", model="test")
        workers = WorkersConfig(pool=(worker,), tools=ToolConfig(network=True, web_search=True))
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="ok")

        with tempfile.TemporaryDirectory() as directory, patch(
            "optiprofiler_evolve.sandbox.subprocess.run", side_effect=fake_run
        ):
            root = Path(directory)
            result = run_agent(
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(),
                workspace=root / "workspace",
                tools_dir=root / "tools",
                broker=BrokerConnection(
                    "/opt/broker",
                    "/opt/artifacts",
                    "token",
                    root / "broker",
                    root / "artifacts",
                ),
                prompt="probe",
                transcript=root / "transcript.jsonl",
            )

        self.assertEqual(result.returncode, 0)
        create = next(call for call in calls if call[:3] == ["docker", "network", "create"])
        network = create[-1]
        self.assertNotIn("--internal", create)
        docker_run = next(call for call in calls if call[:2] == ["docker", "run"])
        self.assertEqual(docker_run[docker_run.index("--network") + 1], network)
        self.assertNotEqual(network, "bridge")
        self.assertIn(["docker", "network", "rm", network], calls)


if __name__ == "__main__":
    unittest.main()
