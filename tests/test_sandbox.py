from __future__ import annotations

import unittest
from pathlib import Path

from optiprofiler_evolve.broker import BrokerConnection
from optiprofiler_evolve.config import (
    SandboxConfig,
    ToolConfig,
    WorkerConfig,
    WorkersConfig,
)
from optiprofiler_evolve.sandbox import _docker_command


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
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges", joined)
        self.assertIn("--read-only", joined)
        self.assertIn("--network private-network", joined)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("super-secret", joined)


if __name__ == "__main__":
    unittest.main()
