"""Select research candidates using controller-only assessment."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from optiprofiler_evolve.config import EvolveConfig
from optiprofiler_evolve.files import append_json as _append_json
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.solver import tree_hash

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.runtime import RunEnvironment


def require_lockbox(config: EvolveConfig, *, champion: bool, hidden: bool) -> None:
    """Apply one policy to planned core stages and, later, committed typed results."""
    if config.data.split.lockbox_required and champion:
        if not hidden or not config.assessment.enabled("performance", "hidden"):
            raise ValueError("A required lockbox needs hidden performance evaluation.")


class ValidationSelector:
    def __init__(self, environment: RunEnvironment) -> None:
        self.environment = environment

    def select(self, candidate_ids: Sequence[str], limit: int) -> tuple[str, ...]:
        environment = self.environment
        if limit < 1:
            raise ValueError("Validation selection limit must be positive.")
        environment.commits.begin_validation_selection()
        unique = tuple(dict.fromkeys(str(value) for value in candidate_ids))
        ranked: list[tuple[float, float, str]] = []
        new_queries = 0
        for candidate_id in unique:
            record = environment.state.candidates.get(candidate_id)
            if record is not None:
                if not record.valid:
                    continue
                if tree_hash(record.path) != record.tree_hash:
                    raise ValueError(f"Candidate changed after admission: {candidate_id}")
                ranked.append((record.ranking_score, record.public_score if record.public_score is not None
                               else float("-inf"), candidate_id))
                continue
            handle = environment.state.variant_handles.get(candidate_id)
            if handle is None:
                raise KeyError(
                    f"Validation selection references unknown candidate {candidate_id!r}."
                )
            public = environment.state.variant_public.get(candidate_id)
            if public is None or not public.success:
                raise ValueError(
                    f"Research variant {candidate_id!r} must pass public evaluation "
                    "before integrity review and validation selection."
                )
            base = environment.state.variant_bases.get(candidate_id)
            if tree_hash(handle.path) != handle.tree_hash:
                raise ValueError("Variant changed after materialization.")
            if base is None:
                raise RuntimeError(f"Research variant {candidate_id!r} has no recorded base tree.")
            result = environment.state.variant_validation.get(candidate_id)
            if result is None:
                if environment.state.assessor is None:
                    raise RuntimeError("Validation selection requires a prepared evaluator.")
                result = environment.state.assessor.evaluate(
                    handle.path,
                    "validation",
                    environment.run_dir
                    / "controller"
                    / "research_evaluations"
                    / candidate_id
                    / "validation",
                    candidate_id=candidate_id,
                    parent=base,
                )
                environment.commits.record_validation_evaluation(candidate_id, result)
                new_queries += 1
            if not result.success or result.assessment_score is None:
                continue
            ranked.append((result.assessment_score, public.score if public.score is not None
                           else float("-inf"), candidate_id))
        selected = tuple(
            candidate_id
            for _score, _public_score, candidate_id in sorted(
                ranked,
                key=lambda item: (-item[0], -item[1], item[2]),
            )[:limit]
        )
        query = {
            "candidate_ids": list(unique),
            "selected_ids": list(selected),
            "new_evaluations": new_queries,
            "cumulative_evaluations": environment.state.validation_query_count,
        }
        _append_json(environment.run_dir / "controller" / "validation_queries.jsonl", query)
        _write_json(
            environment.run_dir / "research" / "validation_usage.json",
            {
                "schema": "validation_usage/1",
                "selection_calls": environment.state.validation_selection_count,
                "new_evaluations": environment.state.validation_query_count,
                "last_selection_candidate_count": len(unique),
                "last_selection_new_evaluations": new_queries,
            },
        )
        environment.emit(
            "validation_selection_finished",
            "succeeded",
            data=query,
        )
        return selected
