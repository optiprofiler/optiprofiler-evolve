"""Select research candidates using controller-only assessment."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from optiprofiler_evolve.files import append_json as _append_json
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.solver import tree_hash

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.engine import EvolutionEngine


class ValidationSelector:
    def __init__(self, engine: EvolutionEngine) -> None:
        self.engine = engine

    def select(self, candidate_ids: Sequence[str], limit: int) -> tuple[str, ...]:
        engine = self.engine
        if limit < 1:
            raise ValueError("Validation selection limit must be positive.")
        engine.validation_selection_count += 1
        unique = tuple(dict.fromkeys(str(value) for value in candidate_ids))
        ranked: list[tuple[float, str]] = []
        new_queries = 0
        for candidate_id in unique:
            record = engine.candidates.get(candidate_id)
            if record is not None:
                if not record.valid:
                    continue
                if record.assessment_score is None or tree_hash(record.path) != record.tree_hash:
                    continue
                ranked.append((record.ranking_score, candidate_id))
                continue
            handle = engine.variant_handles.get(candidate_id)
            if handle is None:
                raise KeyError(
                    f"Validation selection references unknown candidate {candidate_id!r}."
                )
            public = engine.variant_public.get(candidate_id)
            if public is None or not public.success:
                raise ValueError(
                    f"Research variant {candidate_id!r} must pass public evaluation "
                    "before integrity review and validation selection."
                )
            base = engine.variant_bases.get(candidate_id)
            if tree_hash(handle.path) != handle.tree_hash:
                raise ValueError("Variant changed after materialization.")
            if base is None:
                raise RuntimeError(f"Research variant {candidate_id!r} has no recorded base tree.")
            result = engine.variant_validation.get(candidate_id)
            if result is None:
                if engine.evaluator is None:
                    raise RuntimeError("Validation selection requires a prepared evaluator.")
                result = engine.evaluator.evaluate(
                    handle.path,
                    "validation",
                    engine.run_dir
                    / "controller"
                    / "research_evaluations"
                    / candidate_id
                    / "validation",
                    candidate_id=candidate_id,
                    parent=base,
                )
                engine.variant_validation[candidate_id] = result
                engine.validation_query_count += 1
                new_queries += 1
            if not result.success or result.assessment_score is None:
                continue
            ranked.append((result.assessment_score, candidate_id))
        selected = tuple(
            candidate_id
            for _score, candidate_id in sorted(
                ranked,
                key=lambda item: (-item[0], item[1]),
            )[:limit]
        )
        query = {
            "candidate_ids": list(unique),
            "selected_ids": list(selected),
            "new_evaluations": new_queries,
            "cumulative_evaluations": engine.validation_query_count,
        }
        _append_json(engine.run_dir / "controller" / "validation_queries.jsonl", query)
        _write_json(
            engine.run_dir / "research" / "validation_usage.json",
            {
                "schema": "validation_usage/1",
                "selection_calls": engine.validation_selection_count,
                "new_evaluations": engine.validation_query_count,
                "last_selection_candidate_count": len(unique),
                "last_selection_new_evaluations": new_queries,
            },
        )
        engine._emit(
            "validation_selection_finished",
            "succeeded",
            data=query,
        )
        return selected
