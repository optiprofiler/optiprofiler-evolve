"""The strategy analysis workflow stage."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from optiprofiler_evolve.agents.contracts import SolverJob
from optiprofiler_evolve.controller.contracts import VariantHandle, VariantRequest
from optiprofiler_evolve.criteria.performance.evidence import EvidenceReader
from optiprofiler_evolve.files import read_json_object, safe_relative_path, write_json
from optiprofiler_evolve.models import CandidateRecord
from optiprofiler_evolve.phases._research import (
    ABLATION_SCHEMA,
    BUNDLE_SCHEMA,
    DIRECTION_SCHEMA,
    STRATEGY_SCHEMA,
    normalize_strategy_cards,
)
from optiprofiler_evolve.phases._support import _prepared, _tool_config, _tool_payload
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import source_diff, tree_hash


class StrategyAnalysisPhase:
    """Extract per-island strategy hypotheses and test executable removals."""

    name = "strategy_analysis"
    requires = frozenset({"prepared", "populations"})
    optional_requires = frozenset({"directions"})
    provides = frozenset({"strategy_cards", "island_bundles"})

    def __init__(
        self,
        *,
        max_strategies: int = 6,
        max_ablations: int = 6,
        min_effect: float = 0.01,
        n_repeats: int = 1,
        evaluation_mode: str = "smoke",
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
        if evaluation_mode not in {"smoke", "public"}:
            raise ValueError("strategy_analysis.evaluation_mode must be smoke or public")
        self.max_strategies = int(max_strategies)
        self.max_ablations = int(max_ablations)
        self.min_effect = float(min_effect)
        self.n_repeats = int(n_repeats)
        self.evaluation_mode = evaluation_mode
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
                    SolverJob(
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
                card["evidence_level"] = (
                    "Observed" if entry["status"] == "succeeded" else "Unverified"
                )
                card["ablation"] = entry

            supported_count = sum(
                card.get("ablation", {}).get("conclusion") == "supported" for card in cards
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
                    "evaluation_mode": self.evaluation_mode,
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
                        card.get("ablation", {}).get("conclusion") == "supported" for card in cards
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
                    mode=("smoke" if self.evaluation_mode == "smoke" else "public_score"),
                )
                for repeat in range(self.n_repeats)
            ]
            ablated_results = [
                context.services.evaluate_public_variant(
                    handle,
                    f"ablation-{strategy_id}-r{repeat + 1}",
                    mode=("smoke" if self.evaluation_mode == "smoke" else "public_score"),
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
                "evaluation_mode": self.evaluation_mode,
                "measured_base_public_scores": [result.score for result in base_results],
                "ablated_public_scores": [result.score for result in ablated_results],
                "base_artifact_dirs": [str(result.output_dir) for result in base_results],
                "ablated_artifact_dirs": [str(result.output_dir) for result in ablated_results],
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
            card for card in cards if card.get("ablation", {}).get("conclusion") == "supported"
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
            if isinstance(card.get("toggle"), Mapping) and card["toggle"]["kind"] == "removal_patch"
        ]
        removal_patches = [island_dir / str(card["toggle"]["ref"]) for card in prunable]
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


def _populations(value: object) -> tuple[tuple[CandidateRecord, ...], ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, tuple) for item in value):
        raise TypeError("populations artifact has an invalid shape")
    return value


def _candidate_path(run_dir: Path, candidate_id: str | None) -> Path | None:
    if candidate_id is None:
        return None
    path = run_dir / "candidates" / candidate_id
    return path if path.is_dir() else None


def _public_evidence_dir(run_dir: Path, candidate_id: str) -> Path:
    broker_root = (
        run_dir / "controller" / "brokers" / candidate_id / "artifacts" / "evaluations" / "public"
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
{{"cards":[{{"strategy_id":"i{island}-s1","claim":"...","mechanism":"...",
"pseudocode":["..."],"applicability":["..."],"risks":["..."],
"code_bindings":[{{"file":"solver.py","lines":[1,20]}}],
"toggle":{{"kind":"variant_tree","ref":"variants/i{island}-s1"}},
"portable_patch":null,"depends_on":[],"sources":["source.diff"]}}]}}
If a strategy cannot be toggled safely, keep the card with toggle:null; it will remain
Unverified and cannot be transferred to another island. The controller, not you, assigns
Observed/Inferred/Unverified after real ablations.
"""
