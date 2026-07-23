"""Execute coding-agent harnesses in isolated worker environments."""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .broker import BrokerConnection
from .config import SandboxConfig, WorkerConfig, WorkersConfig
from .harness import build_harness_command
from .traces import (
    finalize_adapter_trace,
    prepare_trace,
    record_sanitized_command,
    record_derived_trace_error,
    render_transcript,
    run_captured_process,
    trace_paths,
)


@dataclass(frozen=True)
class AgentRunResult:
    returncode: int
    transcript: Path
    timed_out: bool = False
    native_trace: Path | None = None
    stderr_trace: Path | None = None
    trace_chunks: Path | None = None
    trace_outcome: Path | None = None
    capture_error: str | None = None
    cancelled: bool = False
    termination_reason: str | None = None


def run_agent(
    *,
    worker: WorkerConfig,
    workers: WorkersConfig,
    sandbox: SandboxConfig,
    workspace: Path,
    tools_dir: Path,
    broker: BrokerConnection,
    prompt: str,
    transcript: Path,
    trace_dir: Path,
    trace_context: Mapping[str, object] | None = None,
    cancellation_event: threading.Event | None = None,
) -> AgentRunResult:
    """Run one coding worker and preserve its complete stdout/stderr transcript."""

    if sandbox.backend == "docker" and worker.provider_gateway is not None:
        raise RuntimeError(
            "Provider gateway routing is configured but its Docker sidecar lifecycle "
            "has not been attached to this worker launch. Refusing direct fallback."
        )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    selected_values = _selected_worker_values(worker)
    worker_network: str | None = None
    container_name: str | None = None
    if sandbox.backend == "docker":
        inner_workspace = Path("/workspace")
        harness = build_harness_command(worker, workers, workers.tools, inner_workspace)
        container_name = f"ope-worker-{uuid.uuid4().hex[:12]}"
        worker_network = f"ope-{uuid.uuid4().hex[:12]}"
        network_command = ["docker", "network", "create"]
        if not workers.tools.network:
            network_command.append("--internal")
        network_command.append(worker_network)
        subprocess.run(
            network_command,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        command = _docker_command(
            workers=workers,
            sandbox=sandbox,
            workspace=workspace,
            tools_dir=tools_dir,
            broker=broker,
            harness=harness,
            network=worker_network,
            container_name=container_name,
            selected_values=selected_values,
        )
        environment = dict(os.environ)
        environment.update(selected_values)
        environment.update(
            {
                "OPTIPROFILER_EVOLVE_BROKER_DIR": broker.directory,
                "OPTIPROFILER_EVOLVE_BROKER_TOKEN": broker.token,
                "OPTIPROFILER_EVOLVE_WORKSPACE": "/workspace",
            }
        )
        cwd = None
    else:
        harness = build_harness_command(worker, workers, workers.tools, workspace)
        command = harness
        environment = _worker_environment(selected_values, broker, tools_dir, workspace)
        cwd = workspace

    if trace_dir.exists():
        paths = trace_paths(trace_dir)
        record_sanitized_command(paths, command, selected_values)
    else:
        paths = prepare_trace(
            root=trace_dir,
            prompt=prompt,
            command=command,
            worker=worker,
            workers=workers,
            sandbox=sandbox,
            context=trace_context,
            secret_values=selected_values,
        )
    try:
        completed = run_captured_process(
            command=command,
            prompt=prompt,
            paths=paths,
            timeout_seconds=workers.timeout_seconds,
            environment=environment,
            cwd=cwd,
            cancellation_event=cancellation_event,
        )
        try:
            render_transcript(paths, transcript)
        except (OSError, ValueError) as exc:
            message = f"transcript: {type(exc).__name__}: {exc}"
            record_derived_trace_error(paths, message)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                f"[controller] readable transcript unavailable: {message}\n",
                encoding="utf-8",
            )
        return AgentRunResult(
            completed.returncode,
            transcript,
            timed_out=completed.timed_out,
            native_trace=paths.stdout,
            stderr_trace=paths.stderr,
            trace_chunks=paths.chunks,
            trace_outcome=paths.outcome,
            capture_error=completed.capture_error,
            cancelled=completed.cancelled,
            termination_reason=completed.termination_reason,
        )
    except Exception as exc:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        if not transcript.exists():
            transcript.write_text(
                f"[controller] agent execution failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
        finalize_adapter_trace(
            paths,
            transcript=transcript,
            native_trace=paths.stdout if paths.stdout.exists() else None,
            stderr_trace=paths.stderr if paths.stderr.exists() else None,
            returncode=1,
            timed_out=False,
            cancelled=cancellation_event.is_set() if cancellation_event else False,
            capture_error=f"execution: {type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if container_name is not None:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if worker_network is not None:
            subprocess.run(
                ["docker", "network", "rm", worker_network],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def _docker_command(
    *,
    workers: WorkersConfig,
    sandbox: SandboxConfig,
    workspace: Path,
    tools_dir: Path,
    broker: BrokerConnection,
    harness: list[str],
    network: str,
    container_name: str,
    selected_values: Mapping[str, str],
) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--name",
        container_name,
        "--init",
        "--network",
        network,
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
        "/tmp:rw,nosuid,size=2g",
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


def _worker_environment(
    selected_values: Mapping[str, str],
    broker: BrokerConnection,
    tools_dir: Path,
    workspace: Path,
) -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": f"{tools_dir}:{os.environ.get('PATH', '')}",
        "OPTIPROFILER_EVOLVE_BROKER_DIR": broker.directory,
        "OPTIPROFILER_EVOLVE_BROKER_TOKEN": broker.token,
        "OPTIPROFILER_EVOLVE_WORKSPACE": str(workspace.resolve()),
    }
    environment.update(selected_values)
    return environment


def _selected_worker_values(worker: WorkerConfig) -> Mapping[str, str]:
    values = dict(worker.env)
    for key in worker.pass_env:
        if key not in os.environ:
            raise ValueError(f"Required worker environment variable {key!r} is not set.")
        values[key] = os.environ[key]
    return values


__all__: list[str] = []
