"""The recombine workflow stage."""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from optiprofiler_evolve.controller.contracts import VariantHandle, VariantRequest
from optiprofiler_evolve.files import read_json_object, write_json
from optiprofiler_evolve.phases._research import RECOMBINATION_SCHEMA
from optiprofiler_evolve.phases._support import _prepared, _require_public_performance
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import tree_hash


class RecombinePhase:
    """Test a bounded set of conflict-free cross-island strategy combinations."""

    name = "recombine"
    requires = frozenset({"prepared", "island_bundles", "strategy_cards"})
    provides = frozenset({"combinations"})

    def __init__(
        self,
        *,
        max_strategies: int = 8,
        max_combination_size: int = 2,
        max_combinations: int = 12,
        beam_width: int = 3,
    ) -> None:
        if min(max_strategies, max_combination_size, max_combinations, beam_width) < 1:
            raise ValueError("recombine limits must be positive")
        self.max_strategies = int(max_strategies)
        self.max_combination_size = int(max_combination_size)
        self.max_combinations = int(max_combinations)
        self.beam_width = int(beam_width)

    def validate_config(self, config) -> None:
        _require_public_performance(config, self.name)

    def run(self, context: PhaseContext) -> PhaseResult:
        bundles_payload = read_json_object(Path(str(context.artifacts["island_bundles"])))
        bundles = bundles_payload.get("bundles", [])
        if not isinstance(bundles, list) or not bundles:
            return self._skip(context, "no island bundles")
        bundle_ids = [
            str(item["candidate_id"])
            for item in bundles
            if isinstance(item, Mapping) and item.get("candidate_id")
        ]
        selected_base = context.services.select_by_validation(bundle_ids, 1)
        if not selected_base:
            return self._skip(context, "no validation-selected base")
        base = next(
            item
            for item in bundles
            if isinstance(item, Mapping) and item.get("candidate_id") == selected_base[0]
        )
        base_path = Path(str(base["candidate_path"]))
        base_island = int(base["island"])
        seed_hash = tree_hash(_prepared(context).seed)

        portable: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for path in context.artifacts["strategy_cards"]:
            payload = read_json_object(Path(str(path)))
            island = int(payload["island"])
            for card in payload.get("cards", []):
                if not isinstance(card, Mapping):
                    continue
                if card.get("ablation", {}).get("conclusion") != "supported":
                    continue
                patch = card.get("portable_patch")
                if not isinstance(patch, Mapping) or not patch.get("ref"):
                    continue
                strategy_id = str(card["strategy_id"])
                if island == base_island:
                    excluded.append(
                        {"strategy_id": strategy_id, "reason": "already represented by base island"}
                    )
                    continue
                if patch.get("base") != "seed" or patch.get("base_tree_hash") != seed_hash:
                    excluded.append(
                        {"strategy_id": strategy_id, "reason": "portable base declaration mismatch"}
                    )
                    continue
                portable.append(
                    {
                        "strategy_id": strategy_id,
                        "island": island,
                        "patch": Path(str(path)).parent / str(patch["ref"]),
                        "declared_base": patch.get("base", "seed"),
                        "declared_base_tree_hash": patch.get("base_tree_hash", ""),
                    }
                )
        portable = sorted(portable, key=lambda item: (item["island"], item["strategy_id"]))[
            : self.max_strategies
        ]
        if not portable:
            return self._skip(
                context,
                "no verified portable strategy patches",
                {"base_candidate": base["candidate_id"], "excluded": excluded},
            )

        combinations = []
        handles: dict[str, tuple[VariantHandle, float]] = {}
        specs = []
        for size in range(1, min(self.max_combination_size, len(portable)) + 1):
            specs.extend(itertools.combinations(portable, size))
            if len(specs) >= self.max_combinations:
                break
        for index, group in enumerate(specs[: self.max_combinations], start=1):
            combo_id = f"combo-{index:03d}"
            strategy_ids = [str(item["strategy_id"]) for item in group]
            entry: dict[str, Any] = {
                "combo_id": combo_id,
                "base_candidate": base["candidate_id"],
                "strategy_ids": strategy_ids,
                "patches": [str(item["patch"]) for item in group],
            }
            try:
                handle = context.services.materialize_variant(
                    VariantRequest(
                        variant_id=combo_id,
                        base=base_path,
                        patches=tuple(Path(str(item["patch"])) for item in group),
                    )
                )
                result = context.services.evaluate_public_variant(handle, f"{combo_id}-public")
                if not result.success:
                    raise RuntimeError(result.error or "public evaluation failed")
                handles[combo_id] = (handle, result.score)
                entry.update(
                    {
                        "status": "succeeded",
                        "public_score": result.score,
                        "tree_hash": handle.tree_hash,
                    }
                )
            except Exception as exc:
                entry.update(
                    {
                        "status": "patch_conflict_or_gate_rejected",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            combinations.append(entry)

        selected = context.services.select_by_validation(list(handles), self.beam_width)
        for candidate_id in selected:
            handle, public_score = handles[candidate_id]
            context.services.register_finalist(
                handle,
                {
                    "island": -2,
                    "parent_id": base["candidate_id"],
                    "public_score": public_score,
                    "source": "recombine",
                },
            )
        destination = write_json(
            context.run_dir / "research" / "recombination.json",
            {
                "schema": RECOMBINATION_SCHEMA,
                "status": "ok" if handles else "no_valid_combinations",
                "base_candidate": base["candidate_id"],
                "portable_strategy_count": len(portable),
                "excluded": excluded,
                "bounded_design": {
                    "max_combination_size": self.max_combination_size,
                    "max_combinations": self.max_combinations,
                    "beam_width": self.beam_width,
                },
                "combinations": combinations,
                "validation_selected": list(selected),
                "validation_selection_calls": 1 if handles else 0,
            },
        )
        return PhaseResult(artifacts={"combinations": destination})

    @staticmethod
    def _skip(
        context: PhaseContext,
        reason: str,
        extra: Mapping[str, object] | None = None,
    ) -> PhaseResult:
        destination = write_json(
            context.run_dir / "research" / "recombination.json",
            {
                "schema": RECOMBINATION_SCHEMA,
                "status": "skipped",
                "reason": reason,
                **dict(extra or {}),
            },
        )
        return PhaseResult(status="skipped", artifacts={"combinations": destination})
