"""Optional research phases layered around the stable population kernel."""

from __future__ import annotations

import itertools
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..config import ToolConfig
from ..models import CandidateRecord
from ..protocols import (
    AgentJob,
    PhaseContext,
    PhaseResult,
    VariantHandle,
    VariantRequest,
)
from ..research import (
    ABLATION_SCHEMA,
    BUNDLE_SCHEMA,
    CHALLENGER_SCHEMA,
    DIRECTION_SCHEMA,
    RECOMBINATION_SCHEMA,
    STRATEGY_SCHEMA,
    EvidenceReader,
    normalize_direction_cards,
    normalize_strategy_cards,
    read_json_object,
    safe_relative_path,
    source_diff,
    write_json,
)
from ..solver import tree_hash


class DirectionScoutPhase:
    """Create bounded, read-only hypotheses before island exploration."""

    name = "direction_scout"
    requires = frozenset({"prepared"})
    provides = frozenset({"directions"})

    def __init__(
        self,
        *,
        mode: str = "shared",
        guided_islands: Sequence[int] = (),
        max_directions: int = 4,
        worker_index: int = 0,
        timeout_seconds: int | None = None,
        token_budget: int | None = None,
        max_budget_usd: float | None = None,
        tools: Mapping[str, object] | None = None,
        prompt_version: str = "direction-scout/1",
    ) -> None:
        if mode not in {"off", "shared", "per_island"}:
            raise ValueError("direction_scout.mode must be off, shared, or per_island")
        if max_directions < 1:
            raise ValueError("direction_scout.max_directions must be positive")
        self.mode = mode
        self.guided_islands = tuple(int(value) for value in guided_islands)
        self.max_directions = int(max_directions)
        self.worker_index = int(worker_index)
        self.timeout_seconds = timeout_seconds
        self.token_budget = token_budget
        self.max_budget_usd = max_budget_usd
        self.tools = _tool_config(tools, network=True, web_search=True)
        self.prompt_version = prompt_version

    def run(self, context: PhaseContext) -> PhaseResult:
        prepared = _prepared(context)
        island_count = int(prepared["islands"])
        guided = self.guided_islands or tuple(range(max(1, island_count // 2)))
        if any(value < 0 or value >= island_count for value in guided):
            raise ValueError("direction_scout.guided_islands contains an invalid island")
        destination = context.run_dir / "research" / "directions.json"
        payload: dict[str, Any] = {
            "schema": DIRECTION_SCHEMA,
            "mode": self.mode,
            "prompt_template": self.prompt_version,
            "cards": [],
            "assignment": {str(island): None for island in range(island_count)},
            "scouts": [],
            "status": "off" if self.mode == "off" else "running",
        }
        if self.mode == "off":
            write_json(destination, payload)
            return PhaseResult(status="skipped", artifacts={"directions": destination})

        jobs = [None] if self.mode == "shared" else list(guided)
        all_cards: list[dict[str, Any]] = []
        failed = False
        for job_island in jobs:
            suffix = "shared" if job_island is None else f"island-{job_island}"
            prompt = _direction_prompt(
                max_directions=self.max_directions if job_island is None else 1,
                prompt_version=self.prompt_version,
                island=job_island,
            )
            try:
                result = context.services.run_trusted_agent(
                    AgentJob(
                        role="direction-scout",
                        job_id=f"direction-{suffix}",
                        prompt=prompt,
                        inputs={
                            "seed": Path(str(prepared["seed"])),
                            "public_evidence": Path(str(prepared["seed_public_evidence"])),
                            "solver_contract.json": Path(str(prepared["solver_contract"])),
                        },
                        expected_outputs=("direction_cards.json",),
                        worker_index=self.worker_index,
                        tools=self.tools,
                        timeout_seconds=self.timeout_seconds,
                        token_budget=self.token_budget,
                        max_budget_usd=self.max_budget_usd,
                        trace_links={"island": job_island}
                        if job_island is not None
                        else {},
                    )
                )
                cards_path = result.outputs["direction_cards.json"]
                cards = normalize_direction_cards(
                    read_json_object(cards_path),
                    limit=self.max_directions if job_island is None else 1,
                )
                if not cards:
                    raise ValueError("scout returned no direction cards")
            except Exception as exc:
                failed = True
                payload["scouts"].append(
                    {"scope": suffix, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            payload["scouts"].append(
                {
                    "scope": suffix,
                    "status": "succeeded",
                    "worker_index": self.worker_index,
                    "tools": _tool_payload(self.tools),
                    "token_budget": self.token_budget,
                    "timeout_seconds": self.timeout_seconds,
                    "max_budget_usd": self.max_budget_usd,
                    "transcript": str(result.outcome.transcript),
                }
            )
            if job_island is None:
                all_cards.extend(cards)
            else:
                card = dict(cards[0])
                card["card_id"] = f"i{job_island}-{card['card_id']}"
                all_cards.append(card)
                payload["assignment"][str(job_island)] = card["card_id"]

        if self.mode == "shared" and all_cards:
            for index, island in enumerate(guided):
                payload["assignment"][str(island)] = all_cards[index % len(all_cards)]["card_id"]
        payload["cards"] = all_cards
        payload["status"] = "off-fallback" if failed and not all_cards else "partial" if failed else "ok"
        write_json(destination, payload)
        context.emit(
            "directions_ready",
            "succeeded" if all_cards else "skipped",
            {"mode": self.mode, "card_count": len(all_cards), "status": payload["status"]},
        )
        return PhaseResult(artifacts={"directions": destination})


class StrategyAnalysisPhase:
    """Extract per-island strategy hypotheses and test executable removals."""

    name = "strategy_analysis"
    requires = frozenset({"prepared", "populations"})
    provides = frozenset({"strategy_cards", "island_bundles"})

    def __init__(
        self,
        *,
        max_strategies: int = 6,
        max_ablations: int = 6,
        min_effect: float = 0.01,
        n_repeats: int = 1,
        worker_index: int = 0,
        timeout_seconds: int | None = None,
        token_budget: int | None = None,
        max_budget_usd: float | None = None,
        tools: Mapping[str, object] | None = None,
        prompt_version: str = "strategy-analysis/1",
    ) -> None:
        if max_strategies < 1 or max_ablations < 1 or n_repeats < 1:
            raise ValueError("strategy analysis limits and n_repeats must be positive")
        if min_effect < 0:
            raise ValueError("strategy_analysis.min_effect cannot be negative")
        self.max_strategies = int(max_strategies)
        self.max_ablations = int(max_ablations)
        self.min_effect = float(min_effect)
        self.n_repeats = int(n_repeats)
        self.worker_index = int(worker_index)
        self.timeout_seconds = timeout_seconds
        self.token_budget = token_budget
        self.max_budget_usd = max_budget_usd
        # Remote CLI harnesses still need transport access to their model API.
        # Disable search by default without putting the whole container on an
        # internal-only Docker network.
        self.tools = _tool_config(tools, network=True, web_search=False)
        self.prompt_version = prompt_version

    def run(self, context: PhaseContext) -> PhaseResult:
        populations = _populations(context.artifacts["populations"])
        prepared = _prepared(context)
        seed = Path(str(prepared["seed"]))
        analysis_root = context.run_dir / "research" / "analysis"
        card_paths: list[Path] = []
        bundles: list[dict[str, Any]] = []

        for island, population in enumerate(populations):
            valid = [record for record in population if record.valid]
            selected = context.services.select_by_validation(
                [record.candidate_id for record in valid], 1
            )
            if not selected:
                bundles.append(_empty_bundle(island, "no valid finalist"))
                continue
            finalist = next(record for record in valid if record.candidate_id == selected[0])
            island_dir = analysis_root / f"island-{island}"
            island_dir.mkdir(parents=True, exist_ok=True)
            diff_path = island_dir / "source.diff"
            diff_path.write_text(source_diff(seed, finalist.path), encoding="utf-8")
            evidence_dir = _public_evidence_dir(context.run_dir, finalist.candidate_id)
            evidence_index = EvidenceReader().build(
                evidence_dir,
                island_dir / "evidence_manifest.json",
            )
            inputs = {
                "seed": seed,
                "finalist": finalist.path,
                "source.diff": diff_path,
                "evidence_manifest.json": evidence_index,
                "public_evidence": evidence_dir,
            }
            parent = _candidate_path(context.run_dir, finalist.parent_id)
            if parent is not None:
                inputs["parent"] = parent
            transcript = context.run_dir / "transcripts" / f"{finalist.candidate_id}.jsonl"
            if transcript.is_file():
                inputs["exploration_trace.jsonl"] = transcript
            directions = context.artifacts.get("directions")
            if isinstance(directions, Path) and directions.is_file():
                island_direction = _write_island_direction(directions, island, island_dir)
                if island_direction is not None:
                    inputs["direction.json"] = island_direction

            cards: list[dict[str, Any]] = []
            analyst_error: str | None = None
            declaration: str | None = None
            analyst_metadata: dict[str, Any] = {
                "worker_index": self.worker_index,
                "tools": _tool_payload(self.tools),
                "token_budget": self.token_budget,
                "prompt_template": self.prompt_version,
            }
            try:
                result = context.services.run_trusted_agent(
                    AgentJob(
                        role="strategy-analyst",
                        job_id=f"analysis-island-{island}",
                        prompt=_strategy_prompt(
                            island=island,
                            finalist=finalist.candidate_id,
                            max_strategies=self.max_strategies,
                            prompt_version=self.prompt_version,
                        ),
                        inputs=inputs,
                        expected_outputs=("strategy_cards.json",),
                        worker_index=self.worker_index,
                        tools=self.tools,
                        timeout_seconds=self.timeout_seconds,
                        token_budget=self.token_budget,
                        max_budget_usd=self.max_budget_usd,
                        trace_links={
                            "island": island,
                            "candidate_id": finalist.candidate_id,
                            "parent_id": finalist.parent_id,
                        },
                    )
                )
                analyst_payload = read_json_object(result.outputs["strategy_cards.json"])
                declared = analyst_payload.get("not_decomposable")
                if isinstance(declared, Mapping) and declared.get("reason"):
                    declaration = str(declared["reason"])[:500]
                cards = normalize_strategy_cards(
                    analyst_payload,
                    island=island,
                    finalist=finalist.candidate_id,
                    limit=self.max_strategies,
                )
                _canonicalize_toggles(cards, result.workspace, island_dir, seed)
                analyst_metadata["transcript"] = str(result.outcome.transcript)
                analyst_metadata["returncode"] = result.outcome.returncode
            except Exception as exc:
                analyst_error = f"{type(exc).__name__}: {exc}"

            matrix: list[dict[str, Any]] = []
            for card in cards[: self.max_ablations]:
                entry = self._run_ablation(context, finalist, island_dir, card)
                matrix.append(entry)
                card["evidence_level"] = "Observed" if entry["status"] == "succeeded" else "Unverified"
                card["ablation"] = entry

            supported_count = sum(
                card.get("ablation", {}).get("conclusion") == "supported"
                for card in cards
            )
            if analyst_error is not None:
                analysis_status = "analyst_failed"
                analysis_reason = analyst_error
            elif not cards:
                analysis_status = "not_decomposable"
                analysis_reason = declaration or (
                    "analyst returned zero strategy cards without an explicit "
                    "not_decomposable declaration"
                )
            elif supported_count:
                analysis_status = "verified"
                analysis_reason = None
            else:
                analysis_status = "unverified"
                evaluated_cards = [card for card in cards if "ablation" in card]
                untested = len(cards) - len(evaluated_cards)
                failures = [
                    str(card["ablation"].get("error") or "")
                    for card in evaluated_cards
                    if card["ablation"].get("status") != "succeeded"
                ]
                analysis_reason = (
                    f"{len(cards)} strategies proposed; "
                    f"{len(evaluated_cards)} evaluated by leave-one-out ablation, "
                    f"{supported_count} supported"
                    + (
                        f", {untested} untested (max_ablations={self.max_ablations})"
                        if untested
                        else ""
                    )
                    + (
                        f"; first ablation failure: {failures[0]}"
                        if failures and failures[0]
                        else ""
                    )
                )

            cards_path = write_json(
                island_dir / "strategy_cards.json",
                {
                    "schema": STRATEGY_SCHEMA,
                    "island": island,
                    "finalist": finalist.candidate_id,
                    "baselines": ["seed", finalist.parent_id],
                    "prompt_template": self.prompt_version,
                    "analyst": analyst_metadata,
                    "analyst_error": analyst_error,
                    "cards": cards,
                    "diagnostics": {
                        "status": "unavailable",
                        "reason": "Current benchmark bundle has no canonical problem-contrast table.",
                    },
                },
            )
            card_paths.append(cards_path)
            matrix_path = write_json(
                island_dir / "ablation" / "matrix.json",
                {
                    "schema": ABLATION_SCHEMA,
                    "island": island,
                    "base_candidate": finalist.candidate_id,
                    "n_repeats": self.n_repeats,
                    "comparison": "fresh_repeated_evaluations_on_both_sides",
                    "single_measurement": self.n_repeats == 1,
                    "entries": matrix,
                    "status": "populated" if matrix else analysis_status,
                    "reason": analysis_reason,
                },
            )
            bundle = self._select_bundle(
                context,
                island,
                finalist,
                cards,
                matrix_path,
                island_dir,
            )
            bundle["analysis"] = {
                "status": analysis_status,
                "reason": analysis_reason,
                "declared_by_analyst": declaration is not None,
                "trace": {
                    "analyst_job_id": f"analysis-island-{island}",
                    "analyst_transcript": analyst_metadata.get("transcript"),
                    "strategy_cards": str(cards_path),
                    "ablation_matrix": str(matrix_path),
                },
            }
            bundles.append(bundle)
            context.emit(
                "island_analysis_finished",
                "succeeded" if analyst_error is None else "failed",
                {
                    "island": island,
                    "finalist": finalist.candidate_id,
                    "strategy_count": len(cards),
                    "verified_count": sum(
                        card.get("ablation", {}).get("conclusion") == "supported"
                        for card in cards
                    ),
                    "error": analyst_error,
                },
            )

        bundles_path = write_json(
            analysis_root / "island_bundles.json",
            {"schema": BUNDLE_SCHEMA, "bundles": bundles},
        )
        return PhaseResult(
            artifacts={"strategy_cards": tuple(card_paths), "island_bundles": bundles_path}
        )

    def _run_ablation(
        self,
        context: PhaseContext,
        finalist: CandidateRecord,
        island_dir: Path,
        card: dict[str, Any],
    ) -> dict[str, Any]:
        strategy_id = str(card["strategy_id"])
        toggle = card.get("toggle")
        if not isinstance(toggle, Mapping):
            return {
                "strategy_id": strategy_id,
                "status": "failed",
                "conclusion": "unverified",
                "error": "No executable toggle was supplied.",
            }
        reference = island_dir / str(toggle["ref"])
        request = VariantRequest(
            variant_id=f"ablate-i{finalist.island}-{strategy_id}",
            base=finalist.path,
            patches=(reference,) if toggle["kind"] == "removal_patch" else (),
            tree=reference if toggle["kind"] == "variant_tree" else None,
            expected_base_hash=finalist.tree_hash,
        )
        try:
            handle = context.services.materialize_variant(request)
            base_handle = VariantHandle(
                variant_id=f"ablation-base-i{finalist.island}-{strategy_id}",
                path=finalist.path,
                tree_hash=finalist.tree_hash,
                base_tree_hash=finalist.tree_hash,
                change_hashes=(),
            )
            base_results = [
                context.services.evaluate_public_variant(
                    base_handle,
                    f"ablation-base-{strategy_id}-r{repeat + 1}",
                )
                for repeat in range(self.n_repeats)
            ]
            ablated_results = [
                context.services.evaluate_public_variant(
                    handle,
                    f"ablation-{strategy_id}-r{repeat + 1}",
                )
                for repeat in range(self.n_repeats)
            ]
            results = [*base_results, *ablated_results]
            if not all(result.success for result in results):
                raise RuntimeError(next(result.error for result in results if not result.success))
            base = sum(result.score for result in base_results) / len(base_results)
            ablated = sum(result.score for result in ablated_results) / len(ablated_results)
            effect = base - ablated
            conclusion = (
                "supported"
                if effect >= self.min_effect
                else "contradicted"
                if effect <= -self.min_effect
                else "placebo"
            )
            return {
                "strategy_id": strategy_id,
                "status": "succeeded",
                "conclusion": conclusion,
                "recorded_base_public_score": finalist.public_score,
                "measured_base_public_scores": [result.score for result in base_results],
                "ablated_public_scores": [result.score for result in ablated_results],
                "mean_effect_when_present": effect,
                "min_effect": self.min_effect,
                "variant_id": handle.variant_id,
                "tree_hash": handle.tree_hash,
            }
        except Exception as exc:
            return {
                "strategy_id": strategy_id,
                "status": "failed",
                "conclusion": "unverified",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _select_bundle(
        self,
        context: PhaseContext,
        island: int,
        finalist: CandidateRecord,
        cards: list[dict[str, Any]],
        matrix_path: Path,
        island_dir: Path,
    ) -> dict[str, Any]:
        supported = [
            card
            for card in cards
            if card.get("ablation", {}).get("conclusion") == "supported"
        ]
        rejected = [
            card
            for card in cards
            if card.get("ablation", {}).get("conclusion") in {"placebo", "contradicted"}
        ]
        unverified = [card for card in cards if card not in supported and card not in rejected]
        prunable = [
            card
            for card in rejected
            if isinstance(card.get("toggle"), Mapping)
            and card["toggle"]["kind"] == "removal_patch"
        ]
        removal_patches = [
            island_dir / str(card["toggle"]["ref"])
            for card in prunable
        ]
        unprunable = [card for card in rejected if card not in prunable]
        selected_candidate = finalist.candidate_id
        materialization = "original_finalist"
        selection_ids = [finalist.candidate_id]
        validation_selection_calls = 0
        if removal_patches:
            try:
                handle = context.services.materialize_variant(
                    VariantRequest(
                        variant_id=f"bundle-island-{island}",
                        base=finalist.path,
                        patches=tuple(removal_patches),
                        expected_base_hash=finalist.tree_hash,
                    )
                )
                public = context.services.evaluate_public_variant(handle, "bundle-public")
                if public.success:
                    selection_ids.append(handle.variant_id)
                    selected = context.services.select_by_validation(selection_ids, 1)
                    validation_selection_calls = 1
                    if selected and selected[0] == handle.variant_id:
                        selected_candidate = context.services.register_finalist(
                            handle,
                            {
                                "island": island,
                                "parent_id": finalist.candidate_id,
                                "public_score": public.score,
                                "source": "strategy_analysis",
                            },
                        )
                        materialization = "pruned_removal_patches"
            except Exception as exc:
                materialization = f"pruning_failed:{type(exc).__name__}:{exc}"
        removed = prunable if materialization == "pruned_removal_patches" else []
        retained = [card for card in cards if card not in removed]
        return {
            "island": island,
            "source_finalist": finalist.candidate_id,
            "candidate_id": selected_candidate,
            "candidate_path": str(
                finalist.path
                if selected_candidate == finalist.candidate_id
                else context.run_dir / "research" / "variants" / selected_candidate
            ),
            "supported_strategy_ids": [card["strategy_id"] for card in supported],
            "rejected_strategy_ids": [card["strategy_id"] for card in rejected],
            "unverified_strategy_ids": [card["strategy_id"] for card in unverified],
            "unprunable_strategy_ids": [card["strategy_id"] for card in unprunable],
            "kept_strategy_ids": [card["strategy_id"] for card in retained],
            "dropped_strategy_ids": [card["strategy_id"] for card in removed],
            "selection_policy": "controller_validation_top1",
            "selection_candidates": selection_ids,
            "validation_selection_calls": validation_selection_calls,
            "materialization": materialization,
            "ablation_matrix": str(matrix_path),
        }


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
        seed_hash = tree_hash(Path(str(_prepared(context)["seed"])))

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


class ChallengerPhase:
    """Report a post-selection public comparison against a strong solver."""

    name = "challenger"
    requires = frozenset({"final"})
    provides = frozenset({"challenger_report"})

    def __init__(self, *, reference: str = "scipy_powell") -> None:
        if reference not in {"initial", "scipy_powell", "prima_newuoa"}:
            raise ValueError(
                "challenger.reference must be initial, scipy_powell, or prima_newuoa"
            )
        self.reference = reference

    def run(self, context: PhaseContext) -> PhaseResult:
        final_solver = context.run_dir / "final_solver"
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


def _prepared(context: PhaseContext) -> Mapping[str, object]:
    value = context.artifacts.get("prepared")
    if not isinstance(value, Mapping):
        raise RuntimeError("research phase requires the structured prepared artifact")
    return value


def _populations(value: object) -> tuple[tuple[CandidateRecord, ...], ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, tuple) for item in value):
        raise TypeError("populations artifact has an invalid shape")
    return value


def _tool_config(
    value: Mapping[str, object] | None,
    *,
    network: bool,
    web_search: bool,
) -> ToolConfig:
    defaults: dict[str, object] = {
        "preset": "research",
        "network": network,
        "web_search": web_search,
        "shell": True,
        "python": True,
        "git": True,
        "compilers": False,
        "package_install": False,
        "communication": "none",
    }
    defaults.update(dict(value or {}))
    tools = ToolConfig(**defaults)
    tools.validate()
    return tools


def _tool_payload(tools: ToolConfig) -> dict[str, object]:
    return {
        "preset": tools.preset,
        "web_search": tools.web_search,
        "network": tools.network,
        "shell": tools.shell,
        "python": tools.python,
        "git": tools.git,
        "compilers": tools.compilers,
        "package_install": tools.package_install,
        "communication": tools.communication,
    }


def _candidate_path(run_dir: Path, candidate_id: str | None) -> Path | None:
    if candidate_id is None:
        return None
    path = run_dir / "candidates" / candidate_id
    return path if path.is_dir() else None


def _public_evidence_dir(run_dir: Path, candidate_id: str) -> Path:
    broker_root = (
        run_dir
        / "controller"
        / "brokers"
        / candidate_id
        / "artifacts"
        / "evaluations"
        / "public"
    )
    detailed = sorted(path for path in broker_root.glob("*") if path.is_dir())
    if detailed:
        return detailed[-1]
    return run_dir / "controller" / "evaluations" / candidate_id / "public"


def _write_island_direction(source: Path, island: int, destination_root: Path) -> Path | None:
    payload = read_json_object(source)
    assignment = payload.get("assignment", {})
    if not isinstance(assignment, Mapping):
        return None
    card_id = assignment.get(str(island))
    if card_id is None:
        return None
    cards = payload.get("cards", [])
    card = next(
        (
            dict(value)
            for value in cards
            if isinstance(value, Mapping) and str(value.get("card_id")) == str(card_id)
        ),
        None,
    )
    if card is None:
        return None
    return write_json(
        destination_root / "direction.json",
        {"schema": DIRECTION_SCHEMA, "island": island, "card": card},
    )


def _canonicalize_toggles(
    cards: list[dict[str, Any]],
    workspace: Path,
    island_dir: Path,
    seed: Path,
) -> None:
    for card in cards:
        strategy_id = str(card["strategy_id"])
        toggle = card.get("toggle")
        if not isinstance(toggle, dict):
            card["materialization_error"] = "No executable toggle was supplied."
            continue
        try:
            source = (workspace / safe_relative_path(str(toggle["ref"]))).resolve()
            if workspace.resolve() not in source.parents:
                raise ValueError("toggle path escapes analyst workspace")
            if toggle["kind"] == "removal_patch":
                if not source.is_file():
                    raise ValueError(f"missing removal patch for {strategy_id}")
                destination = island_dir / "toggles" / f"{strategy_id}.patch"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            else:
                if not source.is_dir():
                    raise ValueError(f"missing variant tree for {strategy_id}")
                destination = island_dir / "variants" / strategy_id
                if destination.exists():
                    raise FileExistsError(destination)
                shutil.copytree(source, destination)
            toggle["ref"] = destination.relative_to(island_dir).as_posix()
        except Exception as exc:
            card["toggle"] = None
            card["portable_patch"] = None
            card["materialization_error"] = f"{type(exc).__name__}: {exc}"
            continue

        portable = card.get("portable_patch")
        if not isinstance(portable, dict):
            continue
        portable_source = (workspace / safe_relative_path(str(portable["ref"]))).resolve()
        if workspace.resolve() not in portable_source.parents:
            card["portable_patch"] = None
            continue
        if not portable_source.is_file():
            card["portable_patch"] = None
            continue
        portable_destination = island_dir / "portable" / f"{strategy_id}.patch"
        portable_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(portable_source, portable_destination)
        portable["ref"] = portable_destination.relative_to(island_dir).as_posix()
        if portable.get("base") == "seed" and not portable.get("base_tree_hash"):
            portable["base_tree_hash"] = tree_hash(seed)


def _empty_bundle(island: int, reason: str) -> dict[str, Any]:
    return {
        "island": island,
        "status": "skipped",
        "reason": reason,
        "supported_strategy_ids": [],
        "rejected_strategy_ids": [],
        "unverified_strategy_ids": [],
        "unprunable_strategy_ids": [],
        "kept_strategy_ids": [],
        "dropped_strategy_ids": [],
        "validation_selection_calls": 0,
    }


def _direction_prompt(*, max_directions: int, prompt_version: str, island: int | None) -> str:
    scope = "the shared island portfolio" if island is None else f"island {island}"
    return f"""ROLE: direction-scout
PROMPT_TEMPLATE: {prompt_version}

Read the solver under seed/, its public benchmark evidence, and solver_contract.json.
Research up to {max_directions} genuinely different DFO algorithm directions for {scope}.
You are read-only with respect to the seed. Do not implement or evaluate a solver. Use web
search only for public literature and document every claim with a URL.

Write direction_cards.json with this shape:
{{"cards":[{{"card_id":"d1","title":"...","hypothesis":"...",
"tactics":["..."],"citations":[{{"url":"...","claim":"..."}}]}}]}}
Keep hypotheses independent enough to support an island ablation. Do not claim that a
direction works; state what observable public behavior would support or refute it.
"""


def _strategy_prompt(
    *,
    island: int,
    finalist: str,
    max_strategies: int,
    prompt_version: str,
) -> str:
    return f"""ROLE: strategy-analyst
PROMPT_TEMPLATE: {prompt_version}

Analyze island {island} finalist {finalist}. Compare finalist/, seed/, optional parent/,
source.diff, exploration_trace.jsonl, evidence_manifest.json, and the actual plots/logs under
public_evidence/. Extract at most
{max_strategies} concrete algorithm strategies. Natural-language claims are hypotheses,
not evidence. You cannot call evaluation tools and must not seek validation or hidden data.

If the finalist's change is a single inseparable edit, or there is no real
algorithmic change to decompose, do not invent strategies: write
strategy_cards.json as {{"cards": [], "not_decomposable": {{"reason": "..."}}}}
with a concrete reason grounded in source.diff.

For every strategy, create an executable leave-one-out variant by either:
1. copying finalist/ to variants/<strategy_id>/ and removing only that strategy; or
2. writing a unified removal patch at toggles/<strategy_id>.patch against finalist/.

When a strategy can be expressed as a conflict-aware patch from seed/, optionally add
portable/<strategy_id>.patch and declare it as portable_patch. Do not fabricate a patch.
Write strategy_cards.json with:
{{"cards":[{{"strategy_id":"i{island}-s1","claim":"...",
"code_bindings":[{{"file":"solver.py","lines":[1,20]}}],
"toggle":{{"kind":"variant_tree","ref":"variants/i{island}-s1"}},
"portable_patch":null,"depends_on":[],"sources":["source.diff"]}}]}}
If a strategy cannot be toggled safely, keep the card with toggle:null; it will remain
Unverified and cannot enter recombination. The controller, not you, assigns
Observed/Inferred/Unverified after real ablations.
"""


__all__: list[str] = []
