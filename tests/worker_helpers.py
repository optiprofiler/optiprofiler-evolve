"""Synthetic function-backed workers used by controller tests."""

from dataclasses import fields

from optiprofiler_evolve.shared.protocols import WorkerOutcome, WorkerRequest


class RunnerAdapter:
    name = "synthetic-runner"

    def __init__(self, runner):
        self.runner = runner

    def run(self, request: WorkerRequest) -> WorkerOutcome:
        return self.runner(
            **{field.name: getattr(request, field.name) for field in fields(request)}
        )

    def provenance(self, workers):
        return {
            "adapter": self.name,
            "models": sorted({worker.model for worker in workers}),
        }
