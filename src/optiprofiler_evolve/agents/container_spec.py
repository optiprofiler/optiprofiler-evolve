"""One worker boundary, rendered for direct Docker or Harbor Compose."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.agents.broker import BrokerConnection
from optiprofiler_evolve.config import SandboxConfig


@dataclass(frozen=True)
class _SubscriptionLogin:
    """An owner login file: mounted read-only, copied to the invocation's tmpfs home."""

    mount: str
    home_variable: str
    home: str
    file_name: str
    conflicts: tuple[str, ...]


# Only a path ever enters the environment, so launch/compose/inspect evidence holds no secret.
SUBSCRIPTION_LOGINS = {
    "CODEX_AUTH_JSON_PATH": _SubscriptionLogin(
        "/run/ope-codex-auth.json", "CODEX_HOME", "/tmp/home/.codex", "auth.json",
        ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_FORCE_AUTH_JSON")),
    "CLAUDE_CREDENTIALS_JSON_PATH": _SubscriptionLogin(
        "/run/ope-claude-credentials.json", "CLAUDE_CONFIG_DIR", "/tmp/home/.claude",
        ".credentials.json",
        ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
         "CLAUDE_CODE_OAUTH_TOKEN")),
}


def subscription_login(names) -> str | None:
    """Name the subscription login variable among worker environment names, if any."""
    return next((key for key in SUBSCRIPTION_LOGINS if key in names), None)


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
        key = subscription_login(self.environment)
        if key is None:
            return list(harness)
        login = SUBSCRIPTION_LOGINS[key]
        return ["sh", "-ec", f'export {login.home_variable}={login.home}; '
                f'mkdir -p "${login.home_variable}"; '
                f'cp {login.mount} "${login.home_variable}/{login.file_name}"; '
                f'chmod 600 "${login.home_variable}/{login.file_name}"; exec "$@"',
                "subscription", *harness]

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
    key = subscription_login(values)
    if key is not None:
        login = SUBSCRIPTION_LOGINS[key]
        auth = Path(values[key]).expanduser().resolve(strict=True)
        if not auth.is_file():
            raise ValueError(f"{key} must point to a regular login file.")
        if any(name in values for name in login.conflicts) or sum(
            name in values for name in SUBSCRIPTION_LOGINS
        ) > 1:
            raise ValueError("Subscription authentication cannot be mixed with API routing or FORCE_AUTH.")
        values[key] = login.mount
        auth_mounts = (WorkerMount(str(auth), login.mount, True),)
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
