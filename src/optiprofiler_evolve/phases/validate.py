"""Validate stage and its typed inputs/results."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import CandidateRecord
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import tree_hash


@dataclass(frozen=True)
class ValidateContext(PhaseContext):
    populations: tuple[tuple[CandidateRecord, ...], ...]
    research_finalists: tuple[CandidateRecord, ...]
    fixed_finalists: Callable[[list[list[CandidateRecord]]], list[tuple[int, CandidateRecord]]]


@dataclass(frozen=True, kw_only=True)
class ValidateResult(PhaseResult):
    champion: tuple[int, CandidateRecord]


class ValidatePhase:
    """Execute the validate stage through its bounded controller context."""

    name = "validate"
    requires = frozenset(["populations"])
    provides = frozenset(["champion"])

    def run(self, context: ValidateContext) -> ValidateResult:
        def eligible(record: CandidateRecord) -> bool:
            return (record.valid and record.assessment_score is not None
                    and tree_hash(record.path) == record.tree_hash)

        populations = [
            [record for record in population if eligible(record)]
            for population in context.populations
        ]
        finalists = context.fixed_finalists(populations)
        finalists.extend(
            (record.island, record)
            for record in context.research_finalists
            if record.valid and eligible(record)
        )
        finalists = list(
            {record.candidate_id: (island, record) for island, record in finalists}.values()
        )
        if not finalists:
            raise RuntimeError("No valid finalists meet the configured acceptance requirements.")
        champion = max(
            finalists,
            key=lambda item: (
                item[1].ranking_score,
                item[1].iteration,
                item[1].candidate_id,
            ),
        )
        _write_json(
            context.run_dir / "validation_selection.json",
            {
                "champion": champion[1].candidate_id,
                "finalists": [record.candidate_id for _, record in finalists],
            },
        )
        return ValidateResult(artifacts={"champion": champion}, champion=champion)
