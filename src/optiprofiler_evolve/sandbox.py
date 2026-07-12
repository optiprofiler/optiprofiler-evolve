"""Execute coding-agent harnesses in isolated worker environments."""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .broker import BrokerConnection
from .config import SandboxConfig, WorkerConfig, WorkersConfig
from .harness import build_harness_command


@dataclass(frozen=True)
class AgentRunResult:
    returncode: int
    transcript: Path
    timed_out: bool = False


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
) -> AgentRunResult:
    """Run one coding worker and preserve its complete stdout/stderr transcript."""

    transcript.parent.mkdir(parents=True, exist_ok=True)
    selected_values = _selected_worker_values(worker)
    private_network: str | None = None
    container_name: str | None = None
    if sandbox.backend == "docker":
        container_name = f"ope-worker-{uuid.uuid4().hex[:12]}"
        if not workers.tools.network:
            private_network = f"ope-{uuid.uuid4().hex[:12]}"
            subprocess.run(
                ["docker", "network", "create", "--internal", private_network],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
        inner_workspace = Path("/workspace")
        harness = build_harness_command(worker, workers, workers.tools, inner_workspace)
        command = _docker_command(
            workers=workers,
            sandbox=sandbox,
            workspace=workspace,
            tools_dir=tools_dir,
            broker=broker,
            harness=harness,
            network=private_network or "bridge",
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

    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=workers.timeout_seconds,
            check=False,
            env=environment,
            cwd=cwd,
        )
        transcript.write_text(completed.stdout, encoding="utf-8")
        return AgentRunResult(completed.returncode, transcript)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        transcript.write_text(output + "\n[controller] worker timed out\n", encoding="utf-8")
        return AgentRunResult(124, transcript, timed_out=True)
    finally:
        if container_name is not None:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if private_network is not None:
            subprocess.run(
                ["docker", "network", "rm", private_network],
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
