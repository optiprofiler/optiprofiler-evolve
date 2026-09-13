"""Trusted Harbor launcher; native CLI bytes remain owned by the parent capture."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from optiprofiler_evolve.agents.container_spec import WorkerContainerSpec, WorkerMount


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([bkmg]?)b?", value.lower())
    if match is None:
        raise ValueError("Cannot verify worker memory limit.")
    return int(float(match[1]) * 1024 ** {"": 0, "b": 0, "k": 1, "m": 2, "g": 3}[match[2]])


def _verify_boundary(spec: WorkerContainerSpec, actual: dict, *, allow_direct: bool = False) -> None:
    """Check the realized container, not Harbor's network policy label."""
    host = actual["HostConfig"]
    config = actual["Config"]
    mounts = {
        (m["Source"], m["Destination"], bool(m["RW"]))
        for m in actual["Mounts"] if m["Type"] == "bind"
    }
    expected_mounts = {(m.source, m.target, not m.readonly) for m in spec.mounts}
    checks = {
        "network": set(actual["NetworkSettings"]["Networks"]) == {spec.network},
        "mounts": mounts == expected_mounts,
        "extra volumes": all(m["Type"] == "bind" or (
            m["Type"] == "tmpfs" and m["Destination"] == "/tmp"
        ) for m in actual["Mounts"]),
        "readonly root": host["ReadonlyRootfs"] is True,
        "capabilities": set(host["CapDrop"] or ()) == {"ALL"},
        "no added capabilities": not host.get("CapAdd"),
        "no devices": not host.get("Devices") and not host.get("DeviceRequests"),
        "private process namespace": not host.get("PidMode"),
        "private IPC": host.get("IpcMode") == "private",
        "no published ports": not host.get("PortBindings"),
        "no new privileges": any(s.split(":")[0] == "no-new-privileges"
                                 for s in host["SecurityOpt"] or ()),
        "not privileged": host["Privileged"] is False,
        "user": config["User"] == spec.user and spec.user.split(":")[0] not in {"0", "root", ""},
        "workdir": config["WorkingDir"] == "/workspace",
        "pids": host["PidsLimit"] == spec.pids_limit,
        "memory": host["Memory"] == _memory_bytes(spec.memory),
        "cpus": abs(host["NanoCpus"] - round(spec.cpus * 1e9)) <= 1,
        "init": host["Init"] is True,
        "labels": all(config["Labels"].get(k) == v for k, v in spec.labels.items()),
        "environment": all(
            f"{key}={value}" in config["Env"] for key, value in spec.environment.items()
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"Harbor worker boundary mismatch: {', '.join(failed)}")
    network = json.loads(subprocess.check_output(
        ["docker", "network", "inspect", spec.network], timeout=10,
    ))[0]
    if network["Internal"] is not (not allow_direct):
        raise RuntimeError("Harbor worker network does not match the declared egress policy.")


def _compose_literal(value):
    # Compose interpolates dollar signs even in quoted JSON/YAML strings.
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, dict):
        return {k: _compose_literal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_compose_literal(v) for v in value]
    return value


async def _run(request_path: Path) -> int:
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.config import ResourceMode
    from harbor.models.trial.paths import TrialPaths

    request = json.loads(request_path.read_text(encoding="utf-8"))
    values = request["container"]
    values["mounts"] = tuple(WorkerMount(**mount) for mount in values["mounts"])
    spec = WorkerContainerSpec(**values)
    root = request_path.parent
    environment_dir = root / "environment"
    environment_dir.mkdir()
    (environment_dir / "docker-compose.yaml").write_text(
        json.dumps(_compose_literal(spec.compose()), indent=2), encoding="utf-8",
    )
    logger = logging.getLogger("optiprofiler-evolve.harbor")
    logger.propagate = False
    handler = logging.FileHandler(root / "lifecycle.log")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    environment = DockerEnvironment(
        environment_dir=environment_dir, environment_name=spec.name,
        session_id=spec.name, trial_paths=TrialPaths(root / "trial"),
        task_env_config=EnvironmentConfig(docker_image=spec.image),
        mounts=[], persistent_env={}, logger=logger,
        cpu_enforcement_policy=ResourceMode.IGNORE,
        memory_enforcement_policy=ResourceMode.IGNORE,
    )
    try:
        # Harbor owns container lifecycle, but only the native process writes
        # captured stdout/stderr. This preserves non-UTF8 bytes and stream order.
        with (root / "setup.log").open("w") as diagnostic:
            with contextlib.redirect_stdout(diagnostic), contextlib.redirect_stderr(diagnostic):
                await environment.start(force_build=False)
        actual = json.loads(subprocess.check_output(
            ["docker", "inspect", spec.name], timeout=10,
        ))[0]
        (root / "container_inspect.json").write_text(json.dumps(actual, indent=2))
        _verify_boundary(spec, actual, allow_direct=request.get("allow_direct", False))
        child = await asyncio.create_subprocess_exec(
            "docker", "exec", "--interactive", "--user", spec.user,
            "--workdir", "/workspace", spec.name, *request["harness"],
            stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer,
        )
        return await child.wait()
    finally:
        try:
            with (root / "cleanup.log").open("w") as diagnostic:
                with contextlib.redirect_stdout(diagnostic), contextlib.redirect_stderr(diagnostic):
                    await environment.stop(delete=True)
        finally:
            logger.removeHandler(handler)
            handler.close()


def main() -> int:
    return asyncio.run(_run(Path(sys.argv[1])))


if __name__ == "__main__":
    raise SystemExit(main())
