"""Coding-worker adapters kept separate from population logic."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence

from .config import WorkerConfig
from .protocols import WorkerOutcome, WorkerRequest
from .sandbox import run_agent


class CliWorkerAdapter:
    """Run the configured Codex or Claude CLI in the selected isolation mode."""

    name = "cli"

    def run(self, request: WorkerRequest) -> WorkerOutcome:
        result = run_agent(
            worker=request.worker,
            workers=request.workers,
            sandbox=request.sandbox,
            workspace=request.workspace,
            tools_dir=request.tools_dir,
            broker=request.broker,
            prompt=request.prompt,
            transcript=request.transcript,
        )
        return WorkerOutcome(
            returncode=result.returncode,
            transcript=result.transcript,
            timed_out=result.timed_out,
        )

    def provenance(self, workers: Sequence[WorkerConfig]) -> Mapping[str, object]:
        harnesses = sorted({worker.harness for worker in workers})
        return {
            "adapter": self.name,
            "harnesses": {harness: _cli_version(harness) for harness in harnesses},
            "models": sorted({worker.model for worker in workers}),
        }


def _cli_version(command: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip().splitlines()
    return output[0][:300] if output else None


__all__: list[str] = []
