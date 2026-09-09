"""Controller-owned materialization, validation and research registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.engine import EvolutionEngine

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import replace

from optiprofiler_evolve.agents.jobs import safe_identifier as _safe_identifier
from optiprofiler_evolve.controller.contracts import VariantHandle, VariantRequest
from optiprofiler_evolve.controller.patches import apply_patch as _apply_patch
from optiprofiler_evolve.controller.patches import require_inside as _require_inside
from optiprofiler_evolve.controller.patches import validate_patch_paths as _validate_patch_paths
from optiprofiler_evolve.criteria.performance.references import materialize_reference
from optiprofiler_evolve.files import append_json as _append_json
from optiprofiler_evolve.files import file_hash
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import CandidateRecord, EvaluationResult
from optiprofiler_evolve.solver import (
    changed_files,
    copy_solver_tree,
    tree_hash,
    validate_edit_scope,
)


class EngineControllerServices:
    """Engine-owned implementation of the five trusted phase capabilities."""

    def __init__(self, engine: EvolutionEngine) -> None:
        self.engine = engine

    def materialize_variant(self, request: VariantRequest) -> VariantHandle:
        engine = self.engine
        variant_id = _safe_identifier(request.variant_id, "variant_id")
        if request.tree is not None and request.patches:
            raise ValueError("A variant request may provide a tree or patches, not both.")
        base = request.base.resolve()
        if not base.is_dir():
            raise FileNotFoundError(f"Variant base does not exist: {base}")
        base_hash = tree_hash(base)
        if request.expected_base_hash and request.expected_base_hash != base_hash:
            raise ValueError("Variant base tree hash does not match the declared base.")
        destination = engine.run_dir / "research" / "variants" / variant_id
        staging = engine.run_dir / "research" / "variant_staging" / variant_id
        if destination.exists() or staging.exists() or variant_id in engine.variant_handles:
            raise FileExistsError(f"Variant id already exists: {variant_id}")
        change_hashes: list[str] = []
        try:
            if request.tree is not None:
                source_tree = request.tree.resolve()
                _require_inside(source_tree, engine.run_dir / "research")
                copy_solver_tree(source_tree, staging)
                change_hashes.append(tree_hash(source_tree))
            else:
                copy_solver_tree(base, staging)
                for patch in request.patches:
                    patch = patch.resolve()
                    _require_inside(patch, engine.run_dir / "research")
                    if not patch.is_file():
                        raise FileNotFoundError(f"Variant patch does not exist: {patch}")
                    _validate_patch_paths(patch)
                    _apply_patch(staging, patch)
                    change_hashes.append(file_hash(patch))
            engine._validate_candidate(staging)
            changed = changed_files(base, staging)
            if not changed:
                raise ValueError("Executable variant produced no solver-tree change.")
            validate_edit_scope(changed, engine.editable)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(destination)
            handle = VariantHandle(
                variant_id=variant_id,
                path=destination,
                tree_hash=tree_hash(destination),
                base_tree_hash=base_hash,
                change_hashes=tuple(change_hashes),
            )
            engine.variant_handles[variant_id] = handle
            engine.variant_bases[variant_id] = base
            _write_json(
                engine.run_dir / "research" / "variant_manifests" / f"{variant_id}.json",
                {
                    "schema": "variant_manifest/1",
                    "variant_id": variant_id,
                    "base_tree_hash": base_hash,
                    "change_hashes": list(handle.change_hashes),
                    "tree_hash": handle.tree_hash,
                    "changed_files": list(changed),
                },
            )
            engine._emit(
                "variant_materialized",
                "succeeded",
                scope={"variant_id": variant_id},
                data={
                    "base_tree_hash": base_hash,
                    "tree_hash": handle.tree_hash,
                    "change_hashes": list(handle.change_hashes),
                    "changed_files": list(changed),
                },
            )
            return handle
        except Exception as exc:
            if staging.exists():
                shutil.rmtree(staging)
            engine._emit(
                "variant_materialized",
                "failed",
                scope={"variant_id": variant_id},
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise

    def evaluate_public_variant(
        self,
        handle: VariantHandle,
        label: str,
        *,
        reference: str | None = None,
        mode: str = "public_score",
    ) -> EvaluationResult:
        engine = self.engine
        label = _safe_identifier(label, "evaluation label")
        if tree_hash(handle.path) != handle.tree_hash:
            raise ValueError(f"Variant tree changed after materialization: {handle.variant_id}")
        if engine.evaluator is None or engine.data is None:
            raise RuntimeError("Public variant evaluation requires a prepared evaluator.")
        evaluator = engine.evaluator
        reference_path = None
        if mode not in {"smoke", "public_score"}:
            raise ValueError("Research variants may use only smoke or public_score evaluation.")
        evaluation_mode = mode
        if reference is not None:
            if mode != "public_score":
                raise ValueError("Challenger evaluation cannot use smoke mode.")
            challenger_root = engine.run_dir / "controller" / "challengers" / reference
            if not challenger_root.exists():
                ptype_value = engine.data.selection.get("ptype")
                ptype = str(ptype_value) if ptype_value in {"u", "b", "l", "n"} else None
                materialize_reference(
                    initial=engine.initial,
                    destination=challenger_root,
                    interface=engine.interface,
                    config=replace(engine.config.evaluation_config(), reference=reference),
                    ptype=ptype,
                )
            reference_path = challenger_root
            evaluation_mode = "public"
        output_dir = (
            engine.run_dir / "controller" / "research_evaluations" / handle.variant_id / label
        )
        result = evaluator.evaluate(
            handle.path, evaluation_mode, output_dir, candidate_id=handle.variant_id,
            parent=engine.variant_bases.get(handle.variant_id, engine.evaluator.seed),
            reference=reference_path,
        )
        if reference is None and evaluation_mode == "public_score":
            engine.variant_public[handle.variant_id] = result
        engine._emit(
            "research_evaluation_finished",
            "succeeded" if result.success else "failed",
            scope={"variant_id": handle.variant_id, "evaluation": label},
            data={
                "mode": (
                    "challenger_public"
                    if reference
                    else "smoke"
                    if evaluation_mode == "smoke"
                    else "public"
                ),
                "reference": reference or engine.config.evaluation_config().reference,
                "output_dir": str(result.output_dir),
                "success": result.success,
            },
        )
        return result

    def select_by_validation(self, candidate_ids: Sequence[str], limit: int) -> tuple[str, ...]:
        engine = self.engine
        if limit < 1:
            raise ValueError("Validation selection limit must be positive.")
        engine.validation_selection_count += 1
        unique = tuple(dict.fromkeys(str(value) for value in candidate_ids))
        ranked: list[tuple[float, float, str]] = []
        new_queries = 0
        for candidate_id in unique:
            record = engine.candidates.get(candidate_id)
            if record is not None:
                if not record.valid:
                    continue
                if record.assessment_score is None or tree_hash(record.path) != record.tree_hash:
                    continue
                ranked.append((record.ranking_score, 0.0, candidate_id))
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
                    candidate_id=candidate_id, parent=base,
                )
                engine.variant_validation[candidate_id] = result
                engine.validation_query_count += 1
                new_queries += 1
            if not result.success or result.assessment_score is None:
                continue
            ranked.append(
                (
                    result.assessment_score,
                    0.0,
                    candidate_id,
                )
            )
        selected = tuple(
            candidate_id
            for _validation, _public, candidate_id in sorted(
                ranked,
                key=lambda item: (-item[0], -item[1], item[2]),
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

    def register_finalist(
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
        engine.candidates[record.candidate_id] = record
        engine.research_finalists[record.candidate_id] = record
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
