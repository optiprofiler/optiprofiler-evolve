"""One worker boundary, rendered for direct Docker or Harbor Compose."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.agents.broker import BrokerConnection
from optiprofiler_evolve.config import SandboxConfig


@dataclass(frozen=True)
class WorkerMount:
    source: str
    target: str
    readonly: bool = False


@dataclass(frozen=True)
class WorkerContainerSpec:
    image: str
    name: str
    network: str
    user: str
    cpus: float
    memory: str
    pids_limit: int
    mounts: tuple[WorkerMount, ...]
    environment: Mapping[str, str]
    labels: Mapping[str, str]

    def prepare_harness(self, harness: Sequence[str]) -> list[str]:
        if "CODEX_AUTH_JSON_PATH" not in self.environment:
            return list(harness)
        return ["sh", "-ec", 'export CODEX_HOME=/tmp/home/.codex; '
                'mkdir -p "$CODEX_HOME"; '
                'cp /run/ope-codex-auth.json "$CODEX_HOME/auth.json"; '
                'chmod 600 "$CODEX_HOME/auth.json"; exec "$@"', "subscription", *harness]

    def docker_argv(self, harness: Sequence[str]) -> list[str]:
        command = [
            "docker", "run", "--interactive", "--name", self.name, "--init",
            "--network", self.network, "--cap-drop", "ALL", "--security-opt",
            "no-new-privileges", "--read-only", "--pids-limit", str(self.pids_limit),
            "--cpus", str(self.cpus), "--memory", self.memory, "--user", self.user,
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=2g", "--workdir", "/workspace",
        ]
        for key, value in self.labels.items():
            command.extend(["--label", f"{key}={value}"])
        for mount in self.mounts:
            value = f"type=bind,src={mount.source},dst={mount.target}"
            command.extend(["--mount", value + (",readonly" if mount.readonly else "")])
        for key, value in self.environment.items():
            # Auth and broker capabilities travel through process env, not argv.
            command.extend(["--env", f"{key}={value}" if key in {"HOME", "PATH"} else key])
        return [*command, self.image, *self.prepare_harness(harness)]

    def compose(self) -> dict:
        return {
            "services": {"main": {
                "image": self.image, "container_name": self.name,
                "command": ["sleep", "infinity"], "entrypoint": [],
                "init": True, "read_only": True, "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "user": self.user, "working_dir": "/workspace",
                "pids_limit": self.pids_limit, "cpus": self.cpus,
                "mem_limit": self.memory, "tmpfs": ["/tmp:rw,nosuid,nodev,size=2g"],
                "networks": ["worker"], "labels": dict(self.labels),
                "environment": dict(self.environment),
                "volumes": [{
                    "type": "bind", "source": m.source, "target": m.target,
                    "read_only": m.readonly, "bind": {"create_host_path": False},
                } for m in self.mounts],
            }},
            "networks": {"worker": {"external": True, "name": self.network}},
        }


def worker_container_spec(
    *, sandbox: SandboxConfig, workspace: Path, tools_dir: Path,
    broker: BrokerConnection, network: str, container_name: str,
    selected_values: Mapping[str, str], labels: Mapping[str, str],
) -> WorkerContainerSpec:
    values = dict(selected_values)
    auth_mounts = ()
    if "CODEX_AUTH_JSON_PATH" in values:
        auth = Path(values["CODEX_AUTH_JSON_PATH"]).expanduser().resolve(strict=True)
        if not auth.is_file():
            raise ValueError("CODEX_AUTH_JSON_PATH must point to a regular login file.")
        if any(key in values for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_FORCE_AUTH_JSON")):
            raise ValueError("Subscription authentication cannot be mixed with API routing or FORCE_AUTH.")
        values["CODEX_AUTH_JSON_PATH"] = "/run/ope-codex-auth.json"
        auth_mounts = (WorkerMount(str(auth), "/run/ope-codex-auth.json", True),)
    environment = {
        "HOME": "/tmp/home",
        "PATH": "/opt/optiprofiler-evolve/tools:/tmp/home/.local/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "OPTIPROFILER_EVOLVE_BROKER_DIR": broker.directory,
        "OPTIPROFILER_EVOLVE_BROKER_TOKEN": broker.token,
        "OPTIPROFILER_EVOLVE_WORKSPACE": "/workspace",
        **values,
    }
    return WorkerContainerSpec(
        image=sandbox.worker_image, name=container_name, network=network,
        user=f"{os.getuid()}:{os.getgid()}", cpus=sandbox.cpus,
        memory=sandbox.memory, pids_limit=sandbox.pids_limit,
        mounts=(
            WorkerMount(str(workspace.resolve()), "/workspace"),
            WorkerMount(str(tools_dir.resolve()), "/opt/optiprofiler-evolve/tools", True),
            WorkerMount(str(broker.host_directory.resolve()), broker.directory),
            WorkerMount(str(broker.host_artifacts_directory.resolve()), broker.artifacts_directory, True),
        ) + auth_mounts,
        environment=environment, labels=dict(labels),
    )
