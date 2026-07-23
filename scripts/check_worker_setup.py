#!/usr/bin/env python3
"""Validate one configured coding worker, optionally with a live tool-use probe."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from optiprofiler_evolve.broker import BrokerConnection
from optiprofiler_evolve.config import EvolveConfig, WorkerConfig, load_config
from optiprofiler_evolve.harness import build_harness_command
from optiprofiler_evolve.provider_transport import prepare_provider_transport
from optiprofiler_evolve.sandbox import run_agent


ROOT = Path(__file__).resolve().parents[1]
PROBE = {"agent_mode": True, "tool_use": "filesystem"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check one Codex/Claude worker configuration. --live consumes provider quota "
            "and requires the agent to write a probe file."
        )
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    worker = _select_worker(config, args.worker_index)
    _check_required_environment(worker)
    command_worker = _routed_static_worker(worker)
    command = build_harness_command(
        command_worker,
        dataclasses.replace(config.workers, pool=(command_worker,)),
        config.workers.tools,
        Path("/workspace"),
    )
    _check_agent_flags(command_worker, command)
    _check_runtime(config, worker)

    print(f"worker: {worker.harness}:{worker.model}")
    print(f"sandbox: {config.sandbox.backend}")
    print("agent command: " + shlex.join(_redacted_command(worker, command)))
    if worker.provider_gateway is not None:
        print("provider route: controller-owned gateway (credential withheld from worker)")
    print("static worker check: ok")

    if not args.live:
        print("live provider/tool-use probe: skipped (pass --live to run it)")
        return 0

    run_dir = _live_probe(config, worker)
    print(f"live provider/tool-use probe: ok ({run_dir})")
    return 0


def _select_worker(config: EvolveConfig, index: int) -> WorkerConfig:
    if not 0 <= index < len(config.workers.pool):
        raise SystemExit(
            f"worker index {index} is outside the configured pool "
            f"(size {len(config.workers.pool)})"
        )
    return config.workers.pool[index]


def _check_required_environment(worker: WorkerConfig) -> None:
    missing = [name for name in worker.pass_env if name not in os.environ]
    if missing:
        raise SystemExit("missing required worker environment: " + ", ".join(missing))


def _routed_static_worker(worker: WorkerConfig) -> WorkerConfig:
    if worker.provider_gateway is None:
        return worker
    selected = dict(worker.env)
    selected.update({name: os.environ[name] for name in worker.pass_env})
    return prepare_provider_transport(
        worker,
        selected,
        gateway_origin="http://provider-gateway:8080",
    ).worker


def _check_agent_flags(worker: WorkerConfig, command: list[str]) -> None:
    if worker.harness == "codex":
        required = {"exec", "--dangerously-bypass-approvals-and-sandbox", "--json"}
    else:
        required = {"--print", "--permission-mode", "--tools", "stream-json"}
    missing = sorted(required.difference(command))
    if missing:
        raise SystemExit(f"worker command is missing agent-mode flags: {missing}")


def _check_runtime(config: EvolveConfig, worker: WorkerConfig) -> None:
    if config.sandbox.backend == "unsafe_local":
        if shutil.which(worker.harness) is None:
            raise SystemExit(f"{worker.harness!r} is not available on PATH")
        return
    if shutil.which("docker") is None:
        raise SystemExit("Docker is not available on PATH")
    inspected = subprocess.run(
        ["docker", "image", "inspect", config.sandbox.worker_image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if inspected.returncode:
        detail = inspected.stderr.strip().splitlines()
        message = detail[-1] if detail else "image not found"
        raise SystemExit(f"worker image check failed: {message}")
    version = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            worker.harness,
            config.sandbox.worker_image,
            "--version",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if version.returncode:
        raise SystemExit(
            f"{worker.harness!r} is not usable in {config.sandbox.worker_image!r}: "
            f"{version.stdout.strip()}"
        )


def _redacted_command(worker: WorkerConfig, command: list[str]) -> list[str]:
    secret_values = {
        value
        for key, value in worker.env.items()
        if _looks_secret(key) and value
    }
    secret_values.update(
        os.environ[key]
        for key in worker.pass_env
        if key in os.environ and _looks_secret(key) and os.environ[key]
    )
    return [
        _replace_secrets(argument, secret_values)
        for argument in command
    ]


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(
        marker in upper
        for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    )


def _replace_secrets(argument: str, values: set[str]) -> str:
    redacted = argument
    for value in values:
        redacted = redacted.replace(value, "<redacted>")
    return redacted


def _live_probe(config: EvolveConfig, worker: WorkerConfig) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "build" / "worker-preflight" / f"{stamp}-{worker.harness}"
    workspace = run_dir / "workspace"
    tools_dir = run_dir / "tools"
    exchange = run_dir / "broker" / "exchange"
    artifacts = run_dir / "broker" / "artifacts"
    for path in (workspace, tools_dir, exchange, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    docker = config.sandbox.backend == "docker"
    connection = BrokerConnection(
        directory="/opt/optiprofiler-evolve/broker" if docker else str(exchange),
        artifacts_directory=(
            "/opt/optiprofiler-evolve/artifacts" if docker else str(artifacts)
        ),
        token="worker-preflight-no-evaluator",
        host_directory=exchange,
        host_artifacts_directory=artifacts,
    )
    workers = dataclasses.replace(config.workers, pool=(worker,))
    prompt = (
        "This is an agent-mode preflight. Use a filesystem or shell tool to create "
        "/workspace/agent_probe.json containing exactly this JSON object: "
        f"{json.dumps(PROBE, sort_keys=True)}. Read the file back with a tool, verify it, "
        "then finish. Do not merely print the JSON in your final response."
    )
    transcript = run_dir / "transcript.jsonl"
    result = run_agent(
        worker=worker,
        workers=workers,
        sandbox=config.sandbox,
        workspace=workspace,
        tools_dir=tools_dir,
        broker=connection,
        prompt=prompt,
        transcript=transcript,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"live worker exited with {result.returncode}; inspect {result.transcript}"
        )
    probe = workspace / "agent_probe.json"
    if not probe.is_file():
        raise SystemExit(
            f"worker returned successfully but did not use a tool to create {probe}; "
            f"inspect {result.transcript}"
        )
    try:
        payload = json.loads(probe.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid agent probe file: {exc}") from exc
    if payload != PROBE:
        raise SystemExit(f"unexpected agent probe payload: {payload!r}")
    return run_dir


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TypeError, ValueError) as exc:
        print(f"worker configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
