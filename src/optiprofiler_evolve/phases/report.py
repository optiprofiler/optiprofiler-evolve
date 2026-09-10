"""Report stage and its typed inputs/results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import FinalistResult
from optiprofiler_evolve.observability.reports import (
    write_final_report,
    write_final_solver_explanation,
)
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult


@dataclass(frozen=True)
class ReportContext(PhaseContext):
    final_result: FinalistResult
    final_solver: Path


class ReportPhase:
    """Execute the report stage through its bounded controller context."""

    name = "report"
    requires = frozenset(["final", "final_solver"])
    provides = frozenset(["report"])

    def run(self, context: ReportContext) -> PhaseResult:
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
        return PhaseResult(artifacts={"report": context.run_dir / "FINAL_REPORT.md"})
