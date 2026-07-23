from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.broker import BrokerConnection
from optiprofiler_evolve.config import (
    ProviderGatewayConfig,
    SandboxConfig,
    ToolConfig,
    WorkerConfig,
    WorkersConfig,
)
from optiprofiler_evolve.docker_runtime import (
    DockerInvocationRuntime,
    DockerResource,
    recover_gateway_trace,
    remove_managed_resources,
    select_gc_resources,
)


def _completed(command: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class DockerInvocationRuntimeTests(unittest.TestCase):
    def test_transport_setup_failure_writes_valid_launch_manifest_without_hash(self) -> None:
        worker = WorkerConfig(
            harness="codex",
            model="test-model",
            provider_gateway=ProviderGatewayConfig(
                upstream_base_url="https://api.example.test/v1",
                credential_env="OPENAI_API_KEY",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            trace.mkdir()
            (trace / "invocation.json").write_text(
                json.dumps({"trace_id": "trace-setup", "run_id": "run-setup"}),
                encoding="utf-8",
            )
            runtime = DockerInvocationRuntime(
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(),
                selected_values={},
                trace_dir=trace,
            )

            def fake_docker(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                return _completed(command)

            with (
                patch(
                    "optiprofiler_evolve.docker_runtime._docker_run",
                    side_effect=fake_docker,
                ),
                self.assertRaisesRegex(ValueError, "must be supplied"),
            ):
                runtime.start()

            manifest = json.loads(runtime.gateway_manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "launch_failed")
            self.assertIsNone(manifest["upstream_hash"])

    def test_gateway_launch_error_does_not_copy_provider_route_into_trace(self) -> None:
        secret = "super-private-key-12345"
        upstream = "https://private-provider.example/v1"
        worker = WorkerConfig(
            harness="codex",
            model="test-model",
            env={"OPENAI_API_KEY": secret},
            provider_gateway=ProviderGatewayConfig(
                upstream_base_url=upstream,
                credential_env="OPENAI_API_KEY",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            trace.mkdir()
            (trace / "invocation.json").write_text(
                json.dumps({"trace_id": "trace-launch", "run_id": "run-launch"}),
                encoding="utf-8",
            )
            runtime = DockerInvocationRuntime(
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(),
                selected_values=worker.env,
                trace_dir=trace,
            )

            def fake_docker(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if command[:4] in (
                    ["docker", "container", "ls", "-a"],
                    ["docker", "network", "ls", "-q"],
                ):
                    return _completed(command)
                if command[:2] == ["docker", "run"]:
                    raise subprocess.CalledProcessError(
                        125,
                        command,
                        stderr=f"failed to start {upstream} with {secret}",
                    )
                return _completed(command)

            with (
                patch(
                    "optiprofiler_evolve.docker_runtime._docker_run",
                    side_effect=fake_docker,
                ),
                self.assertRaisesRegex(RuntimeError, "could not start"),
            ):
                runtime.start()

            manifest = runtime.gateway_manifest.read_text(encoding="utf-8")
            lifecycle = runtime.lifecycle_log.read_text(encoding="utf-8")
            self.assertNotIn(upstream, manifest)
            self.assertNotIn(secret, manifest)
            self.assertNotIn(upstream, lifecycle)
            self.assertNotIn(secret, lifecycle)

    def test_gateway_worker_is_internal_and_never_receives_real_credential(self) -> None:
        secret = "provider-secret-value"
        upstream = "https://api.example.test/v1"
        upstream_hash = hashlib.sha256(upstream.encode()).hexdigest()
        worker = WorkerConfig(
            harness="codex",
            model="test-model",
            env={"OPENAI_API_KEY": secret},
            provider_gateway=ProviderGatewayConfig(
                upstream_base_url=upstream,
                credential_env="OPENAI_API_KEY",
            ),
        )
        workers = WorkersConfig(
            pool=(worker,),
            tools=ToolConfig(network=True, web_search=True),
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_docker(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            if command[:4] in (
                ["docker", "container", "ls", "-a"],
                ["docker", "network", "ls", "-q"],
            ):
                return _completed(command)
            if command[:3] == ["docker", "inspect", "--format"]:
                return _completed(command, '{"Running": true, "ExitCode": 0}')
            if command[:2] == ["docker", "logs"]:
                return _completed(command, "gateway-log\n")
            return _completed(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / "trace"
            trace.mkdir()
            (trace / "invocation.json").write_text(
                json.dumps({"trace_id": "trace-1", "run_id": "run-1"}),
                encoding="utf-8",
            )
            gateway = trace / "provider_gateway"
            gateway.mkdir()
            (gateway / "ready.json").write_text(
                json.dumps(
                    {
                        "schema": "provider_gateway_ready/1",
                        "base_url": "http://provider-gateway:8080",
                        "upstream_hash": upstream_hash,
                    }
                ),
                encoding="utf-8",
            )
            runtime = DockerInvocationRuntime(
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(),
                selected_values=worker.env,
                trace_dir=trace,
            )
            with patch(
                "optiprofiler_evolve.docker_runtime._docker_run",
                side_effect=fake_docker,
            ):
                routed, values = runtime.start()
                worker_command = runtime.worker_command(
                    workspace=root / "workspace",
                    tools_dir=root / "tools",
                    broker=BrokerConnection(
                        "/opt/broker",
                        "/opt/artifacts",
                        "token",
                        root / "broker",
                        root / "artifacts",
                    ),
                    harness=["codex", "exec"],
                    selected_values=values,
                )
                (gateway / "outcome.json").write_text(
                    json.dumps(
                        {
                            "schema": "provider_gateway_outcome/1",
                            "outcome": "completed",
                            "request_count": 2,
                            "audit_failure": False,
                            "exit_code": 0,
                            "upstream_hash": upstream_hash,
                        }
                    ),
                    encoding="utf-8",
                )
                outcome = runtime.finish_gateway()
                runtime.cleanup()

            self.assertIsNone(routed.provider_gateway)
            self.assertNotIn(secret, json.dumps(dict(routed.env)))
            self.assertNotIn(secret, " ".join(worker_command))
            self.assertEqual(
                worker_command[worker_command.index("--network") + 1],
                runtime.worker_network,
            )
            network_creates = [
                command
                for command, _ in calls
                if command[:3] == ["docker", "network", "create"]
            ]
            internal = next(item for item in network_creates if item[-1] == runtime.worker_network)
            egress = next(item for item in network_creates if item[-1] == runtime.egress_network)
            self.assertIn("--internal", internal)
            self.assertNotIn("--internal", egress)

            gateway_call, gateway_kwargs = next(
                (command, kwargs)
                for command, kwargs in calls
                if command[:2] == ["docker", "run"]
            )
            self.assertNotIn(secret, " ".join(gateway_call))
            sidecar_env = gateway_kwargs.get("env")
            self.assertIsInstance(sidecar_env, dict)
            assert isinstance(sidecar_env, dict)
            self.assertEqual(sidecar_env["OPENAI_API_KEY"], secret)
            self.assertIn("--gw-priority", next(
                command
                for command, _ in calls
                if command[:3] == ["docker", "network", "connect"]
            ))
            assert outcome is not None
            self.assertTrue(outcome.healthy)
            self.assertEqual(outcome.request_count, 2)
            lifecycle = (trace / "docker_lifecycle.jsonl").read_text(encoding="utf-8")
            self.assertIn('"action": "ready"', lifecycle)
            self.assertIn('"action": "remove"', lifecycle)
            self.assertNotIn(secret, lifecycle)
            self.assertNotIn("api.example.test", lifecycle)

    def test_running_worker_is_stopped_before_gateway_shutdown(self) -> None:
        worker = WorkerConfig(harness="codex", model="test-model")
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            trace.mkdir()
            (trace / "invocation.json").write_text(
                json.dumps({"trace_id": "trace-stop", "run_id": "run-stop"}),
                encoding="utf-8",
            )
            runtime = DockerInvocationRuntime(
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(),
                selected_values={},
                trace_dir=trace,
            )

            def fake_docker(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[:3] == ["docker", "inspect", "--format"]:
                    return _completed(command, '{"Running": true, "ExitCode": 0}')
                return _completed(command)

            with patch(
                "optiprofiler_evolve.docker_runtime._docker_run",
                side_effect=fake_docker,
            ):
                runtime.ensure_worker_terminal()

            self.assertIn(
                ["docker", "stop", "--time", "5", runtime.worker_container],
                calls,
            )
            lifecycle = runtime.lifecycle_log.read_text(encoding="utf-8")
            self.assertIn('"kind": "worker"', lifecycle)
            self.assertIn('"action": "stop"', lifecycle)

    def test_gateway_evidence_without_terminal_manifest_recovers_as_interrupted(self) -> None:
        upstream_hash = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            gateway = trace / "provider_gateway"
            gateway.mkdir(parents=True)
            (gateway / "ready.json").write_text(
                json.dumps(
                    {
                        "schema": "provider_gateway_ready/1",
                        "upstream_hash": upstream_hash,
                    }
                ),
                encoding="utf-8",
            )
            (gateway / "requests.jsonl").write_text("{}\n{}\n{", encoding="utf-8")

            first = recover_gateway_trace(trace)
            second = recover_gateway_trace(trace)

            assert first is not None and second is not None
            self.assertEqual(first, second)
            self.assertEqual(first.outcome, "interrupted")
            self.assertEqual(first.request_count, 2)
            self.assertIsNone(first.exit_code)
            self.assertEqual(first.upstream_hash, upstream_hash)

    def test_terminal_sidecar_outcome_is_preserved_during_controller_recovery(self) -> None:
        upstream_hash = "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            gateway = trace / "provider_gateway"
            gateway.mkdir(parents=True)
            (gateway / "outcome.json").write_text(
                json.dumps(
                    {
                        "schema": "provider_gateway_outcome/1",
                        "outcome": "completed",
                        "request_count": 3,
                        "audit_failure": False,
                        "exit_code": 0,
                        "upstream_hash": upstream_hash,
                    }
                ),
                encoding="utf-8",
            )

            outcome = recover_gateway_trace(trace)

            assert outcome is not None
            self.assertEqual(outcome.outcome, "completed")
            self.assertEqual(outcome.request_count, 3)
            self.assertEqual(outcome.exit_code, 0)
            self.assertTrue(outcome.healthy)

    def test_inconsistent_terminal_sidecar_outcome_recovers_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            gateway = trace / "provider_gateway"
            gateway.mkdir(parents=True)
            (gateway / "outcome.json").write_text(
                json.dumps(
                    {
                        "schema": "provider_gateway_outcome/1",
                        "outcome": "completed",
                        "request_count": 3,
                        "audit_failure": False,
                        "exit_code": None,
                        "upstream_hash": "c" * 64,
                    }
                ),
                encoding="utf-8",
            )
            (gateway / "requests.jsonl").write_text("{}\n{}\n", encoding="utf-8")

            outcome = recover_gateway_trace(trace)

            assert outcome is not None
            self.assertEqual(outcome.outcome, "interrupted")
            self.assertEqual(outcome.request_count, 2)
            self.assertIsNone(outcome.exit_code)
            self.assertFalse(outcome.healthy)

    def test_explicit_interrupted_sidecar_outcome_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            gateway = trace / "provider_gateway"
            gateway.mkdir(parents=True)
            (gateway / "outcome.json").write_text(
                json.dumps(
                    {
                        "schema": "provider_gateway_outcome/1",
                        "outcome": "interrupted",
                        "request_count": 1,
                        "inflight_request_count": 1,
                        "audit_failure": False,
                        "exit_code": 1,
                        "upstream_hash": "d" * 64,
                    }
                ),
                encoding="utf-8",
            )

            outcome = recover_gateway_trace(trace)

            assert outcome is not None
            self.assertEqual(outcome.outcome, "interrupted")
            self.assertEqual(outcome.inflight_request_count, 1)
            self.assertEqual(outcome.exit_code, 1)

    def test_gc_continues_after_one_docker_removal_raises(self) -> None:
        now = datetime.now(timezone.utc)
        resources = (
            DockerResource("container", "c1", "worker-1", "run", "t1", now, False),
            DockerResource("network", "n1", "network-1", "run", "t1", now, False),
        )
        calls: list[list[str]] = []

        def fake_docker(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[:3] == ["docker", "rm", "-f"]:
                raise OSError("docker transport unavailable")
            return _completed(command)

        with patch(
            "optiprofiler_evolve.docker_runtime._docker_run",
            side_effect=fake_docker,
        ):
            errors = remove_managed_resources(resources)

        self.assertEqual(len(errors), 1)
        self.assertIn("OSError", errors[0])
        self.assertIn(["docker", "network", "rm", "n1"], calls)

    def test_gc_selection_is_scoped_and_cross_run_is_age_gated(self) -> None:
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=2)
        recent = now - timedelta(seconds=5)
        resources = (
            DockerResource("container", "c1", "worker-1", "run-1", "trace-1", old, False),
            DockerResource("network", "n1", "net-1", "run-1", "trace-1", old, False),
            DockerResource("container", "c2", "worker-2", "run-2", "trace-2", old, False),
            DockerResource("container", "c3", "active", "run-1", "trace-3", old, True),
            DockerResource("network", "n2", "recent", "run-1", "trace-4", recent, False),
        )

        own = select_gc_resources(
            resources,
            now=now,
            run_id="run-1",
            trace_id="trace-1",
            include_active=True,
        )
        self.assertEqual([item.identifier for item in own], ["c1", "n1"])
        with self.assertRaisesRegex(ValueError, "positive older_than_seconds"):
            select_gc_resources(resources, now=now, run_id=None)
        stale_cross_run = select_gc_resources(
            resources,
            now=now,
            run_id=None,
            older_than_seconds=3600,
        )
        self.assertEqual(
            [item.identifier for item in stale_cross_run],
            ["c1", "c2", "n1"],
        )


if __name__ == "__main__":
    unittest.main()
