"""Hidden stage and its typed inputs/results."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from optiprofiler_evolve.criteria.performance.contracts import Evaluator
from optiprofiler_evolve.models import CandidateRecord, FinalistResult
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import copy_solver_tree


@dataclass(frozen=True)
class HiddenContext(PhaseContext):
    champion: tuple[int, CandidateRecord]
    evaluator: Evaluator
    begin_hidden: Callable[[], None]


@dataclass(frozen=True, kw_only=True)
class HiddenResult(PhaseResult):
    artifacts: Mapping[str, object] = field(init=False)
    final_result: FinalistResult
    final_solver: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", {
            "final": self.final_result, "final_solver": self.final_solver,
        })
        super().__post_init__()


class HiddenPhase:
    """Execute the hidden stage through its bounded controller context."""

    name = "hidden"
    requires = frozenset(["prepared", "champion"])
    provides = frozenset(["final", "final_solver"])

    def run(self, context: HiddenContext) -> HiddenResult:
        island, champion = context.champion
        context.begin_hidden()
        final_result = _evaluate_hidden_champion(
            context.run_dir, island, champion, context.evaluator
        )
        if not final_result.success:
            raise RuntimeError(f"Controller-only hidden evaluation failed: {final_result.error}")
        final_solver = context.run_dir / "final_solver"
        copy_solver_tree(champion.path, final_solver)
        return HiddenResult(
            final_result=final_result, final_solver=final_solver,
            status="skipped" if final_result.assessment_status == "skipped" else "succeeded",
        )


def _evaluate_hidden_champion(
    run_dir: Path,
    island: int,
    record: CandidateRecord,
    evaluator: Evaluator,
) -> FinalistResult:
    output_dir = run_dir / "controller" / "final_evaluations" / record.candidate_id
    result = evaluator.evaluate(record.path, "hidden", output_dir)
    skipped = all(item["status"] == "skipped" for item in result.assessment["results"].values())
    return FinalistResult(
        candidate_id=record.candidate_id,
        island=island,
        public_score=record.public_score,
        validation_score=record.validation_score,
        final_score=result.score,
        output_dir=output_dir,
        success=result.success,
        error=result.error,
        assessment_score=record.assessment_score,
        final_assessment_score=result.assessment_score,
        assessment_status="skipped" if skipped else "succeeded" if result.success else "failed",
    )
