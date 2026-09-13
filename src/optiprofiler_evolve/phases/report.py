"""Report stage and its typed inputs/results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.agents.contracts import AgentJob
from optiprofiler_evolve.agents.explanations import (
    capture_solver_explanation,
    explanation_instructions,
)
from optiprofiler_evolve.errors import RunCancelled
from optiprofiler_evolve.files import read_json_object
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import FinalistResult
from optiprofiler_evolve.observability.reports import (
    write_final_report,
    write_final_solver_explanation,
)
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import tree_hash


@dataclass(frozen=True)
class ReportContext(PhaseContext):
    final_result: FinalistResult
    final_solver: Path


class ReportPhase:
    """Execute the report stage through its bounded controller context."""

    name = "report"
    requires = frozenset(["final", "final_solver"])
    provides = frozenset(["report"])

    def __init__(self, *, require_explanation: bool = False, repair_attempts: int = 1,
                 worker_index: int = 0, timeout_seconds: int = 300) -> None:
        if repair_attempts < 0 or repair_attempts > 3 or timeout_seconds < 1:
            raise ValueError("Report repairs must be bounded (0..3) with a positive timeout.")
        self.require_explanation = require_explanation
        self.repair_attempts = repair_attempts
        self.worker_index = worker_index
        self.timeout_seconds = timeout_seconds

    def _ensure_explanation(self, context: ReportContext) -> dict:
        destination = context.run_dir / "explanations" / f"{context.final_result.candidate_id}.json"
        status = {"status": "missing"}
        if destination.is_file():
            try:
                status = read_json_object(destination)
            except ValueError as exc:
                status = {"status": "invalid", "error": str(exc)}
        frozen_hash = tree_hash(context.final_solver)
        # An optional report remains a pure projection. Explicitly requiring a
        # deliverable enables bounded metadata repair, never solver re-selection.
        if status.get("status") != "complete" and self.require_explanation:
            for index in range(self.repair_attempts):
                attempt = context.run_dir / "explanations" / f"final-repair-{index + 1}.json"
                try:
                    result = context.services.run_trusted_agent(AgentJob(
                        role="solver-explainer", job_id=f"final-explanation-{index + 1}",
                        prompt=("Explain the frozen solver in candidate/. Do not edit its code. "
                                "Do not run benchmarks or make performance claims not supported by evidence. "
                                "Describe mechanisms, concise pseudocode, assumptions and limitations. "
                                f"Previous explanation error: {status.get('error', 'missing explanation')}.\n"
                                + explanation_instructions("candidate")),
                        inputs={"candidate": context.final_solver},
                        expected_outputs=("candidate/.optiprofiler_evolve/solver_explanation.json",),
                        worker_index=self.worker_index, timeout_seconds=self.timeout_seconds,
                        trace_links={"candidate_id": context.final_result.candidate_id},
                    ))
                    capture_solver_explanation(result.workspace / "candidate", attempt,
                                               attempt_id=context.final_result.candidate_id)
                    status = read_json_object(attempt)
                    if tree_hash(result.workspace / "candidate") != frozen_hash:
                        raise ValueError("Explainer changed its solver copy; explanation rejected.")
                    if status.get("status") == "complete":
                        _write_json(destination, status)
                        break
                except RunCancelled:
                    raise
                except Exception as exc:
                    status = {"status": "invalid", "error": f"{type(exc).__name__}: {exc}"}
                    _write_json(attempt, status)
        if tree_hash(context.final_solver) != frozen_hash:
            raise ValueError("Final solver changed during report generation.")
        delivery = {"schema": "final_delivery/1", "candidate_id": context.final_result.candidate_id,
                    "solver_tree_hash": frozen_hash, "explanation_status": status.get("status"),
                    "explanation_error": status.get("error"),
                    "explanation_required": self.require_explanation}
        _write_json(context.run_dir / "final_delivery.json", delivery)
        return delivery

    def run(self, context: ReportContext) -> PhaseResult:
        delivery = self._ensure_explanation(context)
        _write_json(context.run_dir / "final_ranking.json", [context.final_result.as_dict()])
        explanation = write_final_solver_explanation(
            context.run_dir,
            context.final_result,
            context.final_solver,
        )
        write_final_report(
            context.run_dir,
            context.final_result,
            [context.final_result],
            context.final_solver,
            explanation,
        )
        complete = delivery["explanation_status"] == "complete" or not self.require_explanation
        return PhaseResult(status="succeeded" if complete else "failed",
                           error=None if complete else "Final explanation is incomplete; solver is preserved.",
                           artifacts={"report": context.run_dir / "FINAL_REPORT.md"})
