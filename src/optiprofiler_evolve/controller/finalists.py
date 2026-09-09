"""Register only assessed, unchanged research finalists."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from optiprofiler_evolve.controller.contracts import VariantHandle
from optiprofiler_evolve.files import append_json as _append_json
from optiprofiler_evolve.models import CandidateRecord
from optiprofiler_evolve.solver import tree_hash

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.engine import EvolutionEngine


class FinalistRegistrar:
    def __init__(self, engine: EvolutionEngine) -> None:
        self.engine = engine

    def register(
        self,
        handle: VariantHandle,
        metadata: Mapping[str, object],
    ) -> str:
        engine = self.engine
        if handle.variant_id not in engine.variant_handles:
            raise ValueError("Only a gated controller variant can be registered as a finalist.")
        if tree_hash(handle.path) != handle.tree_hash:
            raise ValueError("Variant changed after formal assessment.")
        validation = engine.variant_validation.get(handle.variant_id)
        if validation is None or not validation.success or validation.assessment_score is None:
            raise ValueError("A research finalist must pass controller validation first.")
        public = engine.variant_public.get(handle.variant_id)
        public_score = (
            public.score
            if public is not None and public.success
            else float(metadata.get("public_score", 0.0))
        )
        existing = engine.candidates.get(handle.variant_id)
        if existing is not None:
            if existing.tree_hash != handle.tree_hash:
                raise ValueError("Candidate id is already registered with another tree.")
            return existing.candidate_id
        record = CandidateRecord(
            candidate_id=handle.variant_id,
            island=int(metadata.get("island", -2)),
            iteration=engine.config.evolution.iterations + 1,
            attempt_index=len(engine.research_finalists),
            parent_id=str(metadata["parent_id"]) if metadata.get("parent_id") else None,
            path=handle.path,
            tree_hash=handle.tree_hash,
            public_score=public_score,
            validation_score=validation.score,
            selection_metrics=validation.metric_bundle,
            assessment_score=validation.assessment_score,
            assessment=validation.assessment,
            worker="controller:research",
        )
        engine._register_research_finalist(record)
        _append_json(
            engine.run_dir / "research" / "registered_finalists.jsonl",
            {
                "candidate_id": record.candidate_id,
                "island": record.island,
                "parent_id": record.parent_id,
                "tree_hash": record.tree_hash,
                "public_score": record.public_score,
                "source": metadata.get("source"),
            },
        )
        engine._emit(
            "research_finalist_registered",
            "succeeded",
            scope={"variant_id": record.candidate_id, "island": record.island},
            data={"tree_hash": record.tree_hash, "source": metadata.get("source")},
        )
        return record.candidate_id
