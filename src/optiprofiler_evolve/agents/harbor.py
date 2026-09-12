"""Optional Harbor environment lifecycle beneath the existing agent protocol."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version

from optiprofiler_evolve.agents.container_spec import worker_container_spec
from optiprofiler_evolve.agents.docker_runtime import DockerInvocationRuntime
from optiprofiler_evolve.agents.workers import CliWorkerAdapter


class HarborWorkerAdapter(CliWorkerAdapter):
    """Share native CLI, trace and gateway semantics; let Harbor manage the worker."""

    name = "harbor"

    def __init__(self) -> None:
        if sys.version_info < (3, 12):
            raise RuntimeError("The Harbor backend requires Python >=3.12; the CLI backend does not.")
        try:
            installed = version("harbor")
        except PackageNotFoundError as exc:
            raise RuntimeError("Install optiprofiler-evolve[harbor] to use workers.adapter=harbor.") from exc
        if installed != "0.23.0":
            raise RuntimeError(f"Harbor 0.23.0 is required by this adapter; found {installed}.")

    def provenance(self, workers):
        return {**super().provenance(workers), "harbor_version": version("harbor")}


class HarborInvocationRuntime(DockerInvocationRuntime):
    """Keep gateway authority in the parent even if the Harbor launcher crashes."""

    def worker_command(self, *, workspace, tools_dir, broker, harness, selected_values):
        spec = worker_container_spec(
            sandbox=self.sandbox, workspace=workspace, tools_dir=tools_dir, broker=broker,
            network=self.worker_network, container_name=self.worker_container,
            selected_values=selected_values, labels=self._labels("worker"),
        )
        root = self.trace_dir / "harbor"
        root.mkdir(mode=0o700)
        request = root / "launch.json"
        request.write_text(json.dumps({"container": asdict(spec), "harness": list(harness)}))
        request.chmod(0o600)
        return [sys.executable, "-m", "optiprofiler_evolve.agents._harbor_runner", str(request)]
