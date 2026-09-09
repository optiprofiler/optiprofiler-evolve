"""Report stage and its typed inputs/results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import EvolveResult, FinalistResult
from optiprofiler_evolve.observability.reports import (
    write_final_report,
    write_final_solver_explanation,
)
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult


@dataclass(frozen=True)
class ReportContext(PhaseContext):
    final_result: FinalistResult
    final_solver: Path


@dataclass(frozen=True, kw_only=True)
class ReportResult(PhaseResult):
    result: EvolveResult


class ReportPhase:
    """Execute the report stage through its bounded controller context."""

    name = "report"
    requires = frozenset(["final"])
    provides = frozenset(["result", "report"])

    def run(self, context: ReportContext) -> ReportResult:
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
        result = EvolveResult(
            run_dir=context.run_dir,
            best_solver=context.final_solver,
            best_candidate_id=context.final_result.candidate_id,
            public_score=context.final_result.public_score,
            validation_score=context.final_result.validation_score,
            final_score=context.final_result.final_score,
            finalists=(context.final_result,),
            assessment_score=context.final_result.assessment_score,
            final_assessment_score=context.final_result.final_assessment_score,
        )
        return ReportResult(
            artifacts={"result": result, "report": context.run_dir / "FINAL_REPORT.md"},
            result=result,
        )
