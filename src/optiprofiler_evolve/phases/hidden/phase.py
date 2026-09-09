"""The hidden workflow stage."""

from __future__ import annotations

from pathlib import Path

from optiprofiler_evolve.shared.data import DataPlan
from optiprofiler_evolve.shared.models import CandidateRecord, FinalistResult
from optiprofiler_evolve.shared.protocols import (
    Evaluator,
)
from optiprofiler_evolve.shared.solver import (
    copy_solver_tree,
)

from .context import HiddenContext, HiddenResult


class HiddenPhase:
    """Execute the hidden stage through its bounded controller context."""

    name = "hidden"
    requires = frozenset(["champion"])
    provides = frozenset(["final"])

    def run(self, context: HiddenContext) -> HiddenResult:
        island, champion = context.champion
        context.check_complexity(champion.path, {"candidate_id": champion.candidate_id})
        final_result = _evaluate_hidden_champion(
            context.run_dir, island, champion, context.evaluator, context.data
        )
        if not final_result.success:
            raise RuntimeError(f"Controller-only hidden evaluation failed: {final_result.error}")
        final_solver = context.run_dir / "final_solver"
        copy_solver_tree(champion.path, final_solver)
        return HiddenResult(
            artifacts={"final": final_result}, final_result=final_result, final_solver=final_solver
        )


def _evaluate_hidden_champion(
    run_dir: Path,
    island: int,
    record: CandidateRecord,
    evaluator: Evaluator,
    data: DataPlan,
) -> FinalistResult:
    output_dir = run_dir / "controller" / "final_evaluations" / record.candidate_id
    if data.hidden:
        result = evaluator.evaluate(record.path, "hidden", output_dir)
        final_score = result.score if result.success else 0.0
        success = result.success
        error = result.error
    elif data.lockbox_required:
        output_dir.mkdir(parents=True, exist_ok=True)
        final_score = 0.0
        success = False
        error = "required lockbox has no hidden problems"
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        final_score = record.validation_score
        success = True
        error = None
    return FinalistResult(
        candidate_id=record.candidate_id,
        island=island,
        public_score=record.public_score,
        validation_score=record.validation_score,
        final_score=final_score,
        output_dir=output_dir,
        success=success,
        error=error,
    )
