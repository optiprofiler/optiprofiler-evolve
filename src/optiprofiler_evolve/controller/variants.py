"""Materialize and validate private research variants."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from optiprofiler_evolve.agents.jobs import safe_identifier as _safe_identifier
from optiprofiler_evolve.controller.contracts import VariantHandle, VariantRequest
from optiprofiler_evolve.controller.patches import apply_patch as _apply_patch
from optiprofiler_evolve.controller.patches import require_inside as _require_inside
from optiprofiler_evolve.controller.patches import validate_patch_paths as _validate_patch_paths
from optiprofiler_evolve.files import file_hash
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.solver import (
    changed_files,
    copy_solver_tree,
    tree_hash,
    validate_edit_scope,
)

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.engine import EvolutionEngine


class VariantMaterializer:
    def __init__(self, engine: EvolutionEngine) -> None:
        self.engine = engine

    def materialize(self, request: VariantRequest) -> VariantHandle:
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
