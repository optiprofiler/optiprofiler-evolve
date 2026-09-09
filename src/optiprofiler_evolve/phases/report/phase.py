"""The report workflow stage."""

from __future__ import annotations

from optiprofiler_evolve.phases.report.reports import (
    write_final_report,
    write_final_solver_explanation,
)
from optiprofiler_evolve.shared.files import write_json as _write_json
from optiprofiler_evolve.shared.models import EvolveResult

from .context import ReportContext, ReportResult


class ReportPhase:
    """Execute the report stage through its bounded controller context."""

    name = "report"
    requires = frozenset(["final"])
    provides = frozenset(["result", "report"])

    def run(self, context: ReportContext) -> ReportResult:
        complexity = context.check_complexity(
            context.final_solver,
            {"candidate_id": context.final_result.candidate_id},
        )
        if complexity is not None:
            _write_json(
                context.run_dir / "controller" / "complexity" / "final.json", complexity.as_dict()
            )
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
        )
        return ReportResult(
            artifacts={"result": result, "report": context.run_dir / "FINAL_REPORT.md"},
            result=result,
        )
