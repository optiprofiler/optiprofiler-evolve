"""Docker lifecycle for isolated workers and their provider sidecars."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .broker import BrokerConnection
from .config import SandboxConfig, WorkerConfig, WorkersConfig
from .provider_transport import ProviderTransportPlan, prepare_provider_transport


_MANAGED_LABEL = "optiprofiler.evolve.managed"
_RUN_LABEL = "optiprofiler.evolve.run"
_TRACE_LABEL = "optiprofiler.evolve.trace"
_KIND_LABEL = "optiprofiler.evolve.kind"
_GATEWAY_ALIAS = "provider-gateway"
_GATEWAY_PORT = 8080
_GATEWAY_ORIGIN = f"http://{_GATEWAY_ALIAS}:{_GATEWAY_PORT}"
_STATE_MOUNT = "/opt/optiprofiler-evolve/state"


@dataclass(frozen=True)
class GatewayRuntimeOutcome:
    """Controller-side summary of one short-lived provider sidecar."""

    outcome: str
    request_count: int
    audit_failure: bool
    exit_code: int | None
    upstream_hash: str | None
    manifest: Path
    inflight_request_count: int | None = None

    @property
    def healthy(self) -> bool:
        return (
            self.outcome == "completed"
            and not self.audit_failure
            and self.exit_code == 0
        )


@dataclass(frozen=True)
class DockerResource:
    """One labeled Docker object considered by the explicit GC policy."""

    kind: str
    identifier: str
    name: str
    run_id: str
    trace_id: str
    created_at: datetime
    active: bool


@dataclass(frozen=True)
class _RawGatewayTerminal:
    outcome: str
    request_count: int
    inflight_request_count: int
    audit_failure: bool
    exit_code: int | None


class DockerInvocationRuntime:
    """Own all Docker objects belonging to one coding-agent invocation."""

    def __init__(
        self,
        *,
        worker: WorkerConfig,
        workers: WorkersConfig,
        sandbox: SandboxConfig,
        selected_values: Mapping[str, str],
        trace_dir: Path,
    ) -> None:
        self.original_worker = worker
        self.workers = workers
        self.sandbox = sandbox
        self.selected_values = dict(selected_values)
        self.trace_dir = trace_dir
        self.run_id, self.trace_id = _trace_identity(trace_dir)
        suffix = _resource_suffix(self.trace_id)
        self.worker_container = f"ope-worker-{suffix}"
        self.gateway_container = f"ope-gateway-{suffix}"
        self.worker_network = f"ope-worker-net-{suffix}"
        self.egress_network = f"ope-egress-net-{suffix}"
        self.gateway_dir = trace_dir / "provider_gateway"
        self.gateway_ready = self.gateway_dir / "ready.json"
        self.gateway_outcome = self.gateway_dir / "outcome.json"
        self.gateway_manifest = self.gateway_dir / "manifest.json"
        self.gateway_audit = self.gateway_dir / "requests.jsonl"
        self.gateway_log = self.gateway_dir / "container.log"
        self.lifecycle_log = trace_dir / "docker_lifecycle.jsonl"
        self.transport: ProviderTransportPlan | None = None
        self._gateway_finished = False

    @property
    def gateway_enabled(self) -> bool:
        return self.original_worker.provider_gateway is not None

    def start(self) -> tuple[WorkerConfig, dict[str, str]]:
        """Create networks and the gateway, then return worker-visible inputs."""

        stale_errors = cleanup_managed_resources(
            run_id=self.run_id,
            trace_id=self.trace_id,
            include_active=True,
        )
        self._record_lifecycle(
            "stale_cleanup",
            "invocation",
            self.trace_id,
            "failed" if stale_errors else "succeeded",
            errors=stale_errors,
        )
        try:
            if self.gateway_enabled:
                self.transport = prepare_provider_transport(
                    self.original_worker,
                    self.selected_values,
                    gateway_origin=_GATEWAY_ORIGIN,
                )
                self._create_network(self.worker_network, internal=True, kind="worker-network")
                self._create_network(self.egress_network, internal=False, kind="egress-network")
                self._start_gateway(self.transport)
                self._wait_for_gateway()
                return self.transport.worker, dict(self.transport.worker.env)

            self._create_network(
                self.worker_network,
                internal=not self.workers.tools.network,
                kind="worker-network",
            )
            return self.original_worker, dict(self.selected_values)
        except Exception as exc:
            if self.gateway_enabled:
                self._write_controller_manifest(
                    outcome="launch_failed",
                    request_count=_audit_record_count(self.gateway_audit),
                    audit_failure=True,
                    exit_code=self._gateway_exit_code(),
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._gateway_finished = True
            raise

    def worker_command(
        self,
        *,
        workspace: Path,
        tools_dir: Path,
        broker: BrokerConnection,
        harness: Sequence[str],
        selected_values: Mapping[str, str],
    ) -> list[str]:
        """Build the hardened worker command on its invocation-only network."""

        return build_worker_container_command(
            sandbox=self.sandbox,
            workspace=workspace,
            tools_dir=tools_dir,
            broker=broker,
            harness=harness,
            network=self.worker_network,
            container_name=self.worker_container,
            selected_values=selected_values,
            labels=self._labels("worker"),
        )

    def record_worker_started(self) -> None:
        """Record the worker launch boundary before Docker receives the command."""

        self._record_lifecycle(
            "start",
            "worker",
            self.worker_container,
            "running",
        )

    def record_worker_finished(
        self,
        *,
        returncode: int,
        timed_out: bool,
        cancelled: bool,
        termination_reason: str,
    ) -> None:
        """Record the terminal worker process state before sidecar cleanup."""

        self._record_lifecycle(
            "finish",
            "worker",
            self.worker_container,
            "cancelled" if cancelled else "failed" if returncode else "succeeded",
            returncode=returncode,
            timed_out=timed_out,
            termination_reason=termination_reason,
        )

    def ensure_worker_terminal(self) -> None:
        """Stop a worker that survived loss or termination of its Docker client."""

        state = self._container_state(self.worker_container)
        if state is None or not bool(state.get("Running")):
            return
        self._record_lifecycle(
            "stop",
            "worker",
            self.worker_container,
            "running",
        )
        stopped = _docker_run(
            ["docker", "stop", "--time", "5", self.worker_container],
            check=False,
        )
        if stopped.returncode != 0:
            stopped = _docker_run(
                ["docker", "kill", self.worker_container],
                check=False,
            )
        self._record_lifecycle(
            "stop",
            "worker",
            self.worker_container,
            "succeeded" if stopped.returncode == 0 else "failed",
            returncode=stopped.returncode,
        )
        if stopped.returncode != 0:
            raise RuntimeError("Docker could not stop the worker container after CLI exit.")

    def finish_gateway(self) -> GatewayRuntimeOutcome | None:
        """Stop and classify the gateway after the worker has terminated."""

        if not self.gateway_enabled:
            return None
        if self._gateway_finished and self.gateway_manifest.is_file():
            return _read_gateway_manifest(self.gateway_manifest)

        state = self._gateway_state()
        unhealthy_before_stop = state is None or not bool(state.get("Running"))
        if not unhealthy_before_stop:
            unhealthy_before_stop = not self._gateway_health_ok()
        if state is not None and bool(state.get("Running")):
            self._record_lifecycle(
                "stop",
                "gateway",
                self.gateway_container,
                "running",
            )
            stopped = _docker_run(
                ["docker", "stop", "--time", "5", self.gateway_container],
                check=False,
            )
            self._record_lifecycle(
                "stop",
                "gateway",
                self.gateway_container,
                "succeeded" if stopped.returncode == 0 else "failed",
                returncode=stopped.returncode,
            )
        self._capture_gateway_log()
        exit_code = self._gateway_exit_code()
        raw = _read_json(self.gateway_outcome)
        terminal = _validated_raw_gateway_terminal(raw)
        request_count = (
            terminal.request_count
            if terminal is not None
            else _audit_record_count(self.gateway_audit)
        )
        inflight_request_count = (
            terminal.inflight_request_count if terminal is not None else None
        )
        audit_failure = terminal.audit_failure if terminal is not None else True
        upstream_hash = str(
            raw.get("upstream_hash") or self._expected_upstream_hash()
        )
        outcome = terminal.outcome if terminal is not None else "gateway_unavailable"
        if audit_failure:
            outcome = "audit_failed"
        elif outcome == "interrupted":
            pass
        elif unhealthy_before_stop or exit_code not in {0, None}:
            outcome = "gateway_unavailable"
        result = self._write_controller_manifest(
            outcome=outcome,
            request_count=request_count,
            inflight_request_count=inflight_request_count,
            audit_failure=audit_failure,
            exit_code=exit_code,
            upstream_hash=upstream_hash,
            error=(
                "gateway was unhealthy before controller shutdown"
                if unhealthy_before_stop
                else None
            ),
        )
        self._gateway_finished = True
        self._record_lifecycle(
            "finish",
            "gateway",
            self.gateway_container,
            "succeeded" if result.healthy else "failed",
            outcome=result.outcome,
            request_count=result.request_count,
            inflight_request_count=result.inflight_request_count,
            exit_code=result.exit_code,
            audit_failure=result.audit_failure,
        )
        return result

    def cleanup(self) -> tuple[str, ...]:
        """Remove all invocation resources in reverse order without raising."""

        errors: list[str] = []
        for kind, command in (
            ("worker", ["docker", "rm", "-f", self.worker_container]),
            ("gateway", ["docker", "rm", "-f", self.gateway_container]),
            ("egress-network", ["docker", "network", "rm", self.egress_network]),
            ("worker-network", ["docker", "network", "rm", self.worker_network]),
        ):
            try:
                completed = _docker_run(command, check=False)
            except Exception as exc:
                errors.append(f"{kind}: {type(exc).__name__}: {exc}")
                try:
                    self._record_lifecycle(
                        "remove",
                        kind,
                        command[-1],
                        "failed",
                        error_type=type(exc).__name__,
                    )
                except OSError as record_exc:
                    errors.append(
                        "cleanup-lifecycle: "
                        f"{type(record_exc).__name__}: {record_exc}"
                    )
                continue
            missing = _is_missing_resource(completed.stderr)
            if completed.returncode != 0 and not missing:
                errors.append(f"{kind}: {completed.stderr.strip()[:300]}")
            try:
                self._record_lifecycle(
                    "remove",
                    kind,
                    command[-1],
                    (
                        "skipped"
                        if missing
                        else "succeeded"
                        if completed.returncode == 0
                        else "failed"
                    ),
                    returncode=completed.returncode,
                )
            except OSError as exc:
                errors.append(f"cleanup-lifecycle: {type(exc).__name__}: {exc}")
        try:
            _write_private_json(
                self.trace_dir / "docker_cleanup.json",
                {
                    "schema": "docker_cleanup/1",
                    "complete": not errors,
                    "errors": errors,
                },
            )
        except OSError as exc:
            errors.append(f"cleanup-record: {type(exc).__name__}: {exc}")
        return tuple(errors)

    def _create_network(self, name: str, *, internal: bool, kind: str) -> None:
        command = ["docker", "network", "create"]
        if internal:
            command.append("--internal")
        command.extend(_label_args(self._labels(kind)))
        command.append(name)
        try:
            _docker_run(command, check=True)
        except Exception as exc:
            self._record_lifecycle(
                "create",
                kind,
                name,
                "failed",
                error_type=type(exc).__name__,
            )
            raise
        self._record_lifecycle("create", kind, name, "succeeded")

    def _start_gateway(self, plan: ProviderTransportPlan) -> None:
        self.gateway_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.gateway_dir.chmod(0o700)
        route = plan.route
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            self.gateway_container,
            "--init",
            "--network",
            self.worker_network,
            "--network-alias",
            _GATEWAY_ALIAS,
            *_label_args(self._labels("gateway")),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--pids-limit",
            str(self.sandbox.gateway_pids_limit),
            "--cpus",
            str(self.sandbox.gateway_cpus),
            "--memory",
            self.sandbox.gateway_memory,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--mount",
            f"type=bind,src={self.gateway_dir.resolve()},dst={_STATE_MOUNT}",
            "--env",
            plan.credential_env,
            self.sandbox.gateway_image,
            "--listen",
            f"0.0.0.0:{_GATEWAY_PORT}",
            "--protocol",
            route.protocol,
            "--upstream-base-url",
            route.upstream_base_url,
            "--credential-env",
            plan.credential_env,
            "--auth-mode",
            route.auth_mode,
            "--audit-log",
            f"{_STATE_MOUNT}/requests.jsonl",
            "--ready-file",
            f"{_STATE_MOUNT}/ready.json",
            "--outcome-file",
            f"{_STATE_MOUNT}/outcome.json",
            "--advertised-base-url",
            _GATEWAY_ORIGIN,
            "--max-request-bytes",
            str(route.max_request_bytes),
            "--connect-timeout-seconds",
            str(route.connect_timeout_seconds),
            "--response-timeout-seconds",
            str(route.response_timeout_seconds),
        ]
        environment = dict(os.environ)
        environment[plan.credential_env] = route.credential
        try:
            _docker_run(command, check=True, env=environment)
        except Exception as exc:
            self._record_lifecycle(
                "start",
                "gateway",
                self.gateway_container,
                "failed",
                error_type=type(exc).__name__,
                returncode=_exception_returncode(exc),
            )
            raise RuntimeError(
                "Docker could not start the provider gateway sidecar "
                f"(exit_code={_exception_returncode(exc)})."
            ) from exc
        self._record_lifecycle(
            "start",
            "gateway",
            self.gateway_container,
            "succeeded",
        )
        connect = [
            "docker",
            "network",
            "connect",
            "--gw-priority",
            "1",
            self.egress_network,
            self.gateway_container,
        ]
        try:
            _docker_run(connect, check=True)
        except Exception as exc:
            self._record_lifecycle(
                "connect",
                "egress-network",
                self.egress_network,
                "failed",
                error_type=type(exc).__name__,
                returncode=_exception_returncode(exc),
            )
            raise RuntimeError(
                "Docker could not attach the provider gateway to its egress network. "
                "Gateway mode requires Docker Engine 28 or newer with "
                "--gw-priority support "
                f"(exit_code={_exception_returncode(exc)})."
            ) from exc
        self._record_lifecycle(
            "connect",
            "egress-network",
            self.egress_network,
            "succeeded",
        )

    def _wait_for_gateway(self) -> None:
        deadline = time.monotonic() + self.sandbox.gateway_start_timeout_seconds
        while time.monotonic() < deadline:
            state = self._gateway_state()
            if state is not None and not bool(state.get("Running")):
                raise RuntimeError(
                    "Provider gateway exited before becoming ready with code "
                    f"{state.get('ExitCode')}."
                )
            ready = _read_json(self.gateway_ready)
            if ready and self._ready_manifest_matches(ready) and self._gateway_health_ok():
                self._record_lifecycle(
                    "ready",
                    "gateway",
                    self.gateway_container,
                    "succeeded",
                )
                return
            time.sleep(0.1)
        raise TimeoutError("Provider gateway did not become ready before its start timeout.")

    def _ready_manifest_matches(self, ready: Mapping[str, Any]) -> bool:
        return (
            ready.get("schema") == "provider_gateway_ready/1"
            and ready.get("base_url") == _GATEWAY_ORIGIN
            and ready.get("upstream_hash") == self._expected_upstream_hash()
        )

    def _gateway_health_ok(self) -> bool:
        completed = _docker_run(
            [
                "docker",
                "exec",
                self.gateway_container,
                "python",
                "-c",
                (
                    "import urllib.request; "
                    "urllib.request.urlopen('http://127.0.0.1:8080/"
                    "_optiprofiler/health', timeout=2).read()"
                ),
            ],
            check=False,
            timeout=5,
        )
        return completed.returncode == 0

    def _gateway_state(self) -> dict[str, Any] | None:
        return self._container_state(self.gateway_container)

    def _container_state(self, container: str) -> dict[str, Any] | None:
        completed = _docker_run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                container,
            ],
            check=False,
        )
        if completed.returncode != 0:
            return None
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _gateway_exit_code(self) -> int | None:
        state = self._gateway_state()
        if state is None:
            return None
        value = state.get("ExitCode")
        return int(value) if isinstance(value, int) else None

    def _capture_gateway_log(self) -> None:
        completed = _docker_run(
            ["docker", "logs", self.gateway_container],
            check=False,
        )
        content = (completed.stdout + completed.stderr).encode("utf-8", errors="replace")
        _write_private_bytes(self.gateway_log, content)

    def _expected_upstream_hash(self) -> str:
        if self.transport is None:
            return ""
        return hashlib.sha256(
            self.transport.route.upstream_base_url.encode("utf-8")
        ).hexdigest()

    def _write_controller_manifest(
        self,
        *,
        outcome: str,
        request_count: int,
        audit_failure: bool,
        exit_code: int | None,
        inflight_request_count: int | None = None,
        upstream_hash: str | None = None,
        error: str | None = None,
    ) -> GatewayRuntimeOutcome:
        self.gateway_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "schema": "provider_gateway_manifest/1",
            "outcome": outcome,
            "request_count": request_count,
            "inflight_request_count": inflight_request_count,
            "audit_failure": audit_failure,
            "exit_code": exit_code,
            "upstream_hash": (
                upstream_hash or self._expected_upstream_hash() or None
            ),
            "error": error,
        }
        _write_private_json(self.gateway_manifest, payload)
        return _read_gateway_manifest(self.gateway_manifest)

    def _labels(self, kind: str) -> dict[str, str]:
        return {
            _MANAGED_LABEL: "1",
            _RUN_LABEL: self.run_id,
            _TRACE_LABEL: self.trace_id,
            _KIND_LABEL: kind,
        }

    def _record_lifecycle(
        self,
        action: str,
        kind: str,
        name: str,
        status: str,
        **details: object,
    ) -> None:
        _append_private_jsonl(
            self.lifecycle_log,
            {
                "schema": "docker_lifecycle_event/1",
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "action": action,
                "kind": kind,
                "name": name,
                "status": status,
                **details,
            },
        )


def build_worker_container_command(
    *,
    sandbox: SandboxConfig,
    workspace: Path,
    tools_dir: Path,
    broker: BrokerConnection,
    harness: Sequence[str],
    network: str,
    container_name: str,
    selected_values: Mapping[str, str],
    labels: Mapping[str, str],
) -> list[str]:
    """Build the worker Docker command without embedding environment values."""

    command = [
        "docker",
        "run",
        "--interactive",
        "--name",
        container_name,
        "--init",
        "--network",
        network,
        *_label_args(labels),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--pids-limit",
        str(sandbox.pids_limit),
        "--cpus",
        str(sandbox.cpus),
        "--memory",
        sandbox.memory,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=2g",
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,src={workspace.resolve()},dst=/workspace",
        "--mount",
        f"type=bind,src={tools_dir.resolve()},dst=/opt/optiprofiler-evolve/tools,readonly",
        "--mount",
        f"type=bind,src={broker.host_directory},dst={broker.directory}",
        "--mount",
        f"type=bind,src={broker.host_artifacts_directory},dst={broker.artifacts_directory},readonly",
        "--env",
        "HOME=/tmp/home",
        "--env",
        "PATH=/opt/optiprofiler-evolve/tools:/tmp/home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--env",
        "OPTIPROFILER_EVOLVE_BROKER_DIR",
        "--env",
        "OPTIPROFILER_EVOLVE_BROKER_TOKEN",
        "--env",
        "OPTIPROFILER_EVOLVE_WORKSPACE",
    ]
    for key in selected_values:
        command.extend(["--env", key])
    command.append(sandbox.worker_image)
    command.extend(harness)
    return command


def read_gateway_outcome(trace_dir: Path) -> GatewayRuntimeOutcome | None:
    """Read a controller gateway manifest without exposing transport details."""

    manifest = trace_dir / "provider_gateway" / "manifest.json"
    return _read_gateway_manifest(manifest) if manifest.is_file() else None


def recover_gateway_trace(trace_dir: Path) -> GatewayRuntimeOutcome | None:
    """Recover sidecar terminal evidence or mark an unfinished gateway interrupted."""

    existing = read_gateway_outcome(trace_dir)
    if existing is not None:
        return existing
    gateway_dir = trace_dir / "provider_gateway"
    ready = _read_json(gateway_dir / "ready.json")
    raw = _read_json(gateway_dir / "outcome.json")
    audit = gateway_dir / "requests.jsonl"
    if not ready and not raw and not audit.is_file():
        return None
    upstream_hash = raw.get("upstream_hash") or ready.get("upstream_hash")
    if not isinstance(upstream_hash, str) or len(upstream_hash) != 64:
        upstream_hash = None
    terminal = _validated_raw_gateway_terminal(raw)
    request_count = (
        terminal.request_count if terminal is not None else _audit_record_count(audit)
    )
    audit_failure = terminal.audit_failure if terminal is not None else False
    exit_code = terminal.exit_code if terminal is not None else None
    inflight_request_count = (
        terminal.inflight_request_count if terminal is not None else None
    )
    manifest = gateway_dir / "manifest.json"
    _write_private_json(
        manifest,
        {
            "schema": "provider_gateway_manifest/1",
            "outcome": terminal.outcome if terminal is not None else "interrupted",
            "request_count": request_count,
            "inflight_request_count": inflight_request_count,
            "audit_failure": audit_failure,
            "exit_code": exit_code,
            "upstream_hash": upstream_hash,
            "error": (
                "controller recovered the sidecar terminal outcome"
                if terminal is not None
                else "controller recovered gateway evidence without a terminal outcome"
            ),
        },
    )
    return _read_gateway_manifest(manifest)


def list_managed_resources() -> tuple[DockerResource, ...]:
    """Inspect managed containers and networks without removing anything."""

    resources: list[DockerResource] = []
    for kind, list_command, inspect_command in (
        (
            "container",
            [
                "docker",
                "container",
                "ls",
                "-a",
                "-q",
                "--filter",
                f"label={_MANAGED_LABEL}=1",
            ],
            ["docker", "container", "inspect"],
        ),
        (
            "network",
            [
                "docker",
                "network",
                "ls",
                "-q",
                "--filter",
                f"label={_MANAGED_LABEL}=1",
            ],
            ["docker", "network", "inspect"],
        ),
    ):
        listed = _docker_run(list_command, check=True)
        identifiers = [line for line in listed.stdout.splitlines() if line]
        if not identifiers:
            continue
        inspected = _docker_run([*inspect_command, *identifiers], check=True)
        payload = json.loads(inspected.stdout)
        if not isinstance(payload, list):
            raise ValueError("Docker inspect returned a non-list payload.")
        for item in payload:
            resource = _resource_from_inspect(kind, item)
            if resource is not None:
                resources.append(resource)
    return tuple(resources)


def select_gc_resources(
    resources: Sequence[DockerResource],
    *,
    now: datetime,
    run_id: str | None,
    trace_id: str | None = None,
    older_than_seconds: int = 0,
    include_active: bool = False,
) -> tuple[DockerResource, ...]:
    """Select resources under the explicit own-run or age-gated GC policy."""

    if run_id is None and older_than_seconds < 1:
        raise ValueError("Cross-run GC requires a positive older_than_seconds gate.")
    selected: list[DockerResource] = []
    for resource in resources:
        if run_id is not None and resource.run_id != run_id:
            continue
        if trace_id is not None and resource.trace_id != trace_id:
            continue
        age = (now - resource.created_at).total_seconds()
        if age < older_than_seconds:
            continue
        if resource.active and not include_active:
            continue
        selected.append(resource)
    return tuple(
        sorted(selected, key=lambda item: (item.kind != "container", item.name))
    )


def cleanup_managed_resources(
    *,
    run_id: str | None,
    trace_id: str | None = None,
    older_than_seconds: int = 0,
    include_active: bool = False,
) -> tuple[str, ...]:
    """Apply the labeled GC policy and return non-fatal cleanup errors."""

    resources = select_gc_resources(
        list_managed_resources(),
        now=datetime.now(timezone.utc),
        run_id=run_id,
        trace_id=trace_id,
        older_than_seconds=older_than_seconds,
        include_active=include_active,
    )
    return remove_managed_resources(resources)


def remove_managed_resources(
    resources: Sequence[DockerResource],
) -> tuple[str, ...]:
    """Remove a preselected resource set in container-before-network order."""

    ordered = sorted(resources, key=lambda item: (item.kind != "container", item.name))
    errors: list[str] = []
    for resource in ordered:
        command = (
            ["docker", "rm", "-f", resource.identifier]
            if resource.kind == "container"
            else ["docker", "network", "rm", resource.identifier]
        )
        try:
            completed = _docker_run(command, check=False)
        except Exception as exc:
            errors.append(
                f"{resource.kind}:{resource.name}: {type(exc).__name__}: "
                f"exit_code={_exception_returncode(exc)}"
            )
            continue
        if completed.returncode != 0 and not _is_missing_resource(completed.stderr):
            errors.append(f"{resource.kind}:{resource.name}: {completed.stderr.strip()[:300]}")
    return tuple(errors)


def _trace_identity(trace_dir: Path) -> tuple[str, str]:
    invocation = _read_json(trace_dir / "invocation.json")
    trace_id = invocation.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        raise ValueError("Docker worker trace lacks a trace_id.")
    run_id = invocation.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        run_id = f"adhoc-{trace_id}"
    return _label_value(run_id), _label_value(trace_id)


def _resource_suffix(trace_id: str) -> str:
    rendered = re.sub(r"[^a-zA-Z0-9_.-]", "-", trace_id).strip("-.")
    return (rendered or hashlib.sha256(trace_id.encode()).hexdigest())[:20]


def _label_value(value: str) -> str:
    rendered = re.sub(r"[^a-zA-Z0-9_.-]", "-", value).strip("-.")
    if not rendered:
        raise ValueError("Docker label identity cannot be empty.")
    return rendered[:120]


def _label_args(labels: Mapping[str, str]) -> list[str]:
    arguments: list[str] = []
    for key, value in sorted(labels.items()):
        arguments.extend(["--label", f"{key}={value}"])
    return arguments


def _resource_from_inspect(kind: str, item: object) -> DockerResource | None:
    if not isinstance(item, Mapping):
        return None
    labels: object
    active: bool
    if kind == "container":
        config = item.get("Config")
        state = item.get("State")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        active = bool(state.get("Running")) if isinstance(state, Mapping) else False
        name = str(item.get("Name", "")).lstrip("/")
    else:
        labels = item.get("Labels")
        containers = item.get("Containers")
        active = bool(containers) if isinstance(containers, Mapping) else False
        name = str(item.get("Name", ""))
    if not isinstance(labels, Mapping) or labels.get(_MANAGED_LABEL) != "1":
        return None
    run_id = labels.get(_RUN_LABEL)
    trace_id = labels.get(_TRACE_LABEL)
    if not isinstance(run_id, str) or not isinstance(trace_id, str):
        return None
    created = _parse_docker_time(item.get("Created"))
    identifier = str(item.get("Id") or item.get("ID") or "")
    if not identifier or not name:
        return None
    return DockerResource(kind, identifier, name, run_id, trace_id, created, active)


def _parse_docker_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Managed Docker resource lacks a creation timestamp.")
    rendered = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        parsed = datetime.strptime(value.split(".", 1)[0], "%Y-%m-%dT%H:%M:%S")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _read_gateway_manifest(path: Path) -> GatewayRuntimeOutcome:
    payload = _read_json(path)
    if payload.get("schema") != "provider_gateway_manifest/1":
        raise ValueError(f"Invalid provider gateway manifest: {path}")
    outcome = payload.get("outcome")
    if outcome not in {
        "completed",
        "audit_failed",
        "interrupted",
        "gateway_unavailable",
        "launch_failed",
    }:
        raise ValueError(f"Invalid provider gateway outcome: {path}")
    request_count = _nonnegative_int(payload.get("request_count"))
    if request_count is None:
        raise ValueError(f"Invalid provider gateway request count: {path}")
    exit_code = payload.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        raise ValueError(f"Invalid provider gateway exit code: {path}")
    audit_failure = payload.get("audit_failure")
    if not isinstance(audit_failure, bool):
        raise ValueError(f"Invalid provider gateway audit state: {path}")
    upstream_hash = payload.get("upstream_hash")
    if upstream_hash is not None and (
        not isinstance(upstream_hash, str) or len(upstream_hash) != 64
    ):
        raise ValueError(f"Invalid provider gateway upstream hash: {path}")
    inflight_request_count = payload.get("inflight_request_count")
    if inflight_request_count is not None:
        inflight_request_count = _nonnegative_int(inflight_request_count)
        if inflight_request_count is None:
            raise ValueError(f"Invalid provider gateway inflight count: {path}")
    return GatewayRuntimeOutcome(
        outcome=str(outcome),
        request_count=request_count,
        audit_failure=audit_failure,
        exit_code=exit_code,
        upstream_hash=upstream_hash,
        manifest=path,
        inflight_request_count=inflight_request_count,
    )


def _validated_raw_gateway_terminal(
    payload: Mapping[str, Any],
) -> _RawGatewayTerminal | None:
    if payload.get("schema") != "provider_gateway_outcome/1":
        return None
    outcome = payload.get("outcome")
    if outcome not in {"completed", "audit_failed", "interrupted"}:
        return None
    request_count = _nonnegative_int(payload.get("request_count"))
    if request_count is None:
        return None
    inflight_request_count = _nonnegative_int(
        payload.get("inflight_request_count", 0)
    )
    if inflight_request_count is None:
        return None
    audit_failure = payload.get("audit_failure")
    if not isinstance(audit_failure, bool):
        return None
    exit_code = payload.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        return None
    if outcome == "completed" and (
        exit_code != 0 or audit_failure or inflight_request_count != 0
    ):
        return None
    if outcome == "audit_failed" and (
        exit_code in {None, 0} or not audit_failure
    ):
        return None
    if outcome == "interrupted" and exit_code == 0:
        return None
    return _RawGatewayTerminal(
        outcome=str(outcome),
        request_count=request_count,
        inflight_request_count=inflight_request_count,
        audit_failure=audit_failure,
        exit_code=exit_code,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_private_bytes(temporary, encoded)
    temporary.replace(path)
    path.chmod(0o600)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written < 1:
                raise OSError("short private artifact write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_private_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(payload, sort_keys=True, default=str) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written < 1:
                raise OSError("short Docker lifecycle write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _audit_record_count(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    break
                if not isinstance(value, dict):
                    break
                count += 1
    except OSError:
        return count
    return count


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _exception_returncode(exc: BaseException) -> int | None:
    value = getattr(exc, "returncode", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_missing_resource(stderr: str) -> bool:
    lowered = stderr.lower()
    return (
        "no such container" in lowered
        or "no such network" in lowered
        or "not found" in lowered
    )


def _docker_run(
    command: Sequence[str],
    *,
    check: bool,
    env: Mapping[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=dict(env) if env is not None else None,
        timeout=timeout,
    )


__all__: list[str] = []
