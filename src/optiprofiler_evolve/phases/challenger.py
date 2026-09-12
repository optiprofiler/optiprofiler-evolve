"""The challenger workflow stage."""

from __future__ import annotations

from optiprofiler_evolve.controller.contracts import VariantHandle
from optiprofiler_evolve.criteria.performance.competitors import competitor_names
from optiprofiler_evolve.files import write_json
from optiprofiler_evolve.phases._research import CHALLENGER_SCHEMA, _require_public_performance
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import tree_hash


class ChallengerPhase:
    """Report a post-selection public comparison against a strong solver."""

    name = "challenger"
    requires = frozenset({"final", "final_solver"})
    provides = frozenset({"challenger_report"})

    def __init__(self, *, reference: str = "scipy_powell") -> None:
        if reference != "initial" and reference not in competitor_names():
            raise ValueError("challenger.reference must be initial or a registered DFO competitor")
        self.reference = reference

    def validate_config(self, config) -> None:
        _require_public_performance(config, self.name)

    def run(self, context: PhaseContext) -> PhaseResult:
        final_solver = context.artifacts["final_solver"]
        digest = tree_hash(final_solver)
        handle = VariantHandle(
            variant_id="final-champion",
            path=final_solver,
            tree_hash=digest,
            base_tree_hash=digest,
            change_hashes=(),
        )
        result = context.services.evaluate_public_variant(
            handle,
            "challenger-public",
            reference=self.reference,
        )
        destination = write_json(
            context.run_dir / "research" / "challenger_report.json",
            {
                "schema": CHALLENGER_SCHEMA,
                "candidate_id": context.artifacts["final"].candidate_id,
                "candidate_tree_hash": digest,
                "challenger": self.reference,
                "evaluation_mode": "public_post_selection",
                "success": result.success,
                "candidate_score": result.candidate_score,
                "challenger_score": result.reference_score,
                "normalized_pairwise_fitness": result.score,
                "output_dir": str(result.output_dir),
                "selection_effect": "none",
                "comparability_note": (
                    "OptiProfiler scores depend on the competitor set. This report is not "
                    "numerically interchangeable with candidate-vs-seed search scores."
                ),
            },
        )
        return PhaseResult(artifacts={"challenger_report": destination})
