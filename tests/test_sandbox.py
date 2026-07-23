from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from optiprofiler_evolve.broker import BrokerConnection
from optiprofiler_evolve.config import (
    SandboxConfig,
    ToolConfig,
    WorkerConfig,
    WorkersConfig,
)
from optiprofiler_evolve.docker_runtime import (
    GatewayRuntimeOutcome,
    build_worker_container_command,
)
from optiprofiler_evolve.sandbox import run_agent
from optiprofiler_evolve.traces import CapturedProcess


class SandboxCommandTests(unittest.TestCase):
    def test_docker_start_failure_flushes_trace_before_cleanup(self) -> None:
        worker = WorkerConfig(harness="codex", model="test")
        workers = WorkersConfig(pool=(worker,), timeout_seconds=5)
        runtime = MagicMock()
        runtime.gateway_enabled = False
        runtime.start.side_effect = RuntimeError("gateway launch failed")
        runtime.finish_gateway.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workspace").mkdir()

            def assert_trace_flushed() -> tuple[()]:
                outcome = json.loads((root / "trace" / "outcome.json").read_text())
                self.assertEqual(outcome["state"], "failed")
                self.assertIn("gateway launch failed", outcome["capture_error"])
                return ()

            runtime.cleanup.side_effect = assert_trace_flushed
            with (
                patch(
                    "optiprofiler_evolve.sandbox.DockerInvocationRuntime",
                    return_value=runtime,
                ),
                self.assertRaisesRegex(RuntimeError, "gateway launch failed"),
            ):
                run_agent(
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
                    trace_dir=root / "trace",
                )

            runtime.cleanup.assert_called_once_with()

    def test_launch_failure_still_writes_terminal_trace_outcome(self) -> None:
        worker = WorkerConfig(harness="codex", model="test")
        workers = WorkersConfig(pool=(worker,), timeout_seconds=5)
        with tempfile.TemporaryDirectory() as directory, patch(
            "optiprofiler_evolve.sandbox.build_harness_command",
            return_value=[str(Path(directory) / "missing-command")],
        ):
            root = Path(directory)
            (root / "workspace").mkdir()
            with self.assertRaises(FileNotFoundError):
                run_agent(
                    worker=worker,
                    workers=workers,
                    sandbox=SandboxConfig(backend="unsafe_local"),
                    workspace=root / "workspace",
                    tools_dir=root / "tools",
                    broker=BrokerConnection(
                        str(root / "broker"),
                        str(root / "artifacts"),
                        "token",
                        root / "broker",
                        root / "artifacts",
                    ),
                    prompt="probe",
                    transcript=root / "transcript.jsonl",
                    trace_dir=root / "trace",
                )

            outcome = json.loads((root / "trace" / "outcome.json").read_text())
            self.assertEqual(outcome["state"], "launch_failed")
            self.assertIn("FileNotFoundError", outcome["capture_error"])

    def test_gateway_failure_fails_invocation_after_preserving_worker_output(self) -> None:
        worker = WorkerConfig(harness="codex", model="test")
        workers = WorkersConfig(pool=(worker,), timeout_seconds=5)
        runtime = MagicMock()
        runtime.gateway_enabled = True
        runtime.cleanup.return_value = ()
        runtime.start.return_value = (worker, {})
        runtime.worker_command.return_value = [
            sys.executable,
            "-c",
            "print('worker-completed')",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workspace").mkdir()
            manifest = root / "trace" / "provider_gateway" / "manifest.json"
            runtime.finish_gateway.return_value = GatewayRuntimeOutcome(
                outcome="audit_failed",
                request_count=1,
                audit_failure=True,
                exit_code=1,
                upstream_hash="a" * 64,
                manifest=manifest,
            )
            with patch(
                "optiprofiler_evolve.sandbox.DockerInvocationRuntime",
                return_value=runtime,
            ):
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
                    trace_dir=root / "trace",
                )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.provider_gateway_outcome, "audit_failed")
            self.assertIn("worker-completed", result.transcript.read_text())
            outcome = json.loads((root / "trace" / "outcome.json").read_text())
            self.assertEqual(outcome["termination_reason"], "provider_gateway_failure")
            self.assertIn("provider gateway failed", outcome["transport_error"])
            calls = [item[0] for item in runtime.method_calls]
            self.assertLess(
                calls.index("ensure_worker_terminal"),
                calls.index("finish_gateway"),
            )

    def test_gateway_lifecycle_exception_overrides_completed_worker_trace(self) -> None:
        worker = WorkerConfig(harness="codex", model="test")
        workers = WorkersConfig(pool=(worker,), timeout_seconds=5)
        runtime = MagicMock()
        runtime.gateway_enabled = True
        runtime.cleanup.return_value = ()
        runtime.start.return_value = (worker, {})
        runtime.worker_command.return_value = [
            sys.executable,
            "-c",
            "print('worker-completed')",
        ]
        runtime.finish_gateway.side_effect = RuntimeError("gateway outcome unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workspace").mkdir()
            with (
                patch(
                    "optiprofiler_evolve.sandbox.DockerInvocationRuntime",
                    return_value=runtime,
                ),
                self.assertRaisesRegex(RuntimeError, "gateway outcome unavailable"),
            ):
                run_agent(
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
                    trace_dir=root / "trace",
                )

            outcome = json.loads((root / "trace" / "outcome.json").read_text())
            self.assertEqual(outcome["state"], "failed")
            self.assertEqual(outcome["termination_reason"], "provider_gateway_failure")
            self.assertIn("gateway lifecycle failed", outcome["transport_error"])

    def test_local_agent_returns_native_trace_and_readable_transcript(self) -> None:
        worker = WorkerConfig(harness="codex", model="test")
        workers = WorkersConfig(
            pool=(worker,),
            timeout_seconds=5,
            tools=ToolConfig(network=False, web_search=False),
        )
        command = [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print('prompt=' + data); "
            "sys.stderr.write('diagnostic\\n')",
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
            "optiprofiler_evolve.sandbox.build_harness_command", return_value=command
        ):
            root = Path(directory)
            (root / "workspace").mkdir()
            result = run_agent(
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(backend="unsafe_local"),
                workspace=root / "workspace",
                tools_dir=root / "tools",
                broker=BrokerConnection(
                    str(root / "broker"),
                    str(root / "artifacts"),
                    "token",
                    root / "broker",
                    root / "artifacts",
                ),
                prompt="probe",
                transcript=root / "transcript.jsonl",
                trace_dir=root / "trace",
                trace_context={"join": {"attempt_id": "test"}},
            )

            self.assertEqual(result.returncode, 0)
            self.assertIsNotNone(result.native_trace)
            self.assertIsNotNone(result.stderr_trace)
            assert result.native_trace is not None
            assert result.stderr_trace is not None
            self.assertIn(b"prompt=probe", result.native_trace.read_bytes())
            self.assertIn(b"diagnostic", result.stderr_trace.read_bytes())
            transcript = result.transcript.read_text(encoding="utf-8")
            self.assertIn("prompt=probe", transcript)
            self.assertIn("diagnostic", transcript)

    def test_docker_boundary_is_hardened_and_secret_values_are_not_in_argv(self) -> None:
        worker = WorkerConfig(
            harness="codex",
            model="test",
            env={"OPENAI_API_KEY": "super-secret"},
        )
        command = build_worker_container_command(
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
            labels={"optiprofiler.evolve.managed": "1"},
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
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def fake_capture(**kwargs: object) -> CapturedProcess:
            command = kwargs["command"]
            paths = kwargs["paths"]
            assert isinstance(command, list)
            calls.append(command)
            return CapturedProcess(0, False, paths)  # type: ignore[arg-type]

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("optiprofiler_evolve.docker_runtime._docker_run", side_effect=fake_run),
            patch("optiprofiler_evolve.sandbox.run_captured_process", side_effect=fake_capture),
            patch("optiprofiler_evolve.sandbox.render_transcript"),
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
                trace_dir=root / "trace",
                trace_context={"join": {"attempt_id": "test"}},
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
