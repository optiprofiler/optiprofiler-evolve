"""The validate workflow stage."""

from __future__ import annotations

from optiprofiler_evolve.shared.files import write_json as _write_json
from optiprofiler_evolve.shared.models import CandidateRecord

from .context import ValidateContext, ValidateResult


class ValidatePhase:
    """Execute the validate stage through its bounded controller context."""

    name = "validate"
    requires = frozenset(["populations"])
    provides = frozenset(["champion"])

    def run(self, context: ValidateContext) -> ValidateResult:
        def eligible(record: CandidateRecord) -> bool:
            report = context.assess_complexity(record.path, {"candidate_id": record.candidate_id})
            return report is None or not report.blocked

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
                item[1].validation_score,
                item[1].public_score,
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
