"""The strategy transfer workflow stage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from optiprofiler_evolve.agents.contracts import SolverJob
from optiprofiler_evolve.agents.explanations import (
    capture_solver_explanation,
    explanation_instructions,
)
from optiprofiler_evolve.controller.contracts import VariantHandle, VariantRequest
from optiprofiler_evolve.files import read_json_object, write_json
from optiprofiler_evolve.observability.reports import stage_assessment
from optiprofiler_evolve.phases._research import (
    STRATEGY_TRANSFER_SCHEMA,
    _require_public_performance,
    _tool_config,
    supported_strategy_cards,
)
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import copy_solver_tree, tree_hash


class StrategyTransferPhase:
    """Let each island test semantic strategies verified on other islands.

    This phase deliberately transfers explanation cards, not an entire donor
    solver tree. Each target island keeps its own finalist as the base and the
    controller independently evaluates and validates any integrated variant.
    """

    name = "strategy_transfer"
    requires = frozenset({"prepared", "island_bundles", "strategy_cards"})
    provides = frozenset({"strategy_transfer"})

    def __init__(
        self,
        *,
        max_strategies_per_island: int = 3,
        attempts_per_island: int = 1,
        worker_index: int = 0,
        timeout_seconds: int | None = None,
        token_budget: int | None = None,
        max_budget_usd: float | None = None,
        tools: Mapping[str, object] | None = None,
        prompt_version: str = "strategy-transfer/2",
    ) -> None:
        if max_strategies_per_island < 1 or attempts_per_island < 1:
            raise ValueError("strategy transfer limits must be positive")
        self.max_strategies_per_island = int(max_strategies_per_island)
        self.attempts_per_island = int(attempts_per_island)
        self.worker_index = int(worker_index)
        self.timeout_seconds = timeout_seconds
        self.token_budget = token_budget
        self.max_budget_usd = max_budget_usd
        self.tools = _tool_config(tools, network=True, web_search=False)
        self.prompt_version = prompt_version

    def validate_config(self, config) -> None:
        _require_public_performance(config, self.name)

    def run(self, context: PhaseContext) -> PhaseResult:
        payload = read_json_object(Path(str(context.artifacts["island_bundles"])))
        raw_bundles = payload.get("bundles", [])
        bundles = (
            [
                item
                for item in raw_bundles
                if isinstance(item, Mapping)
                and item.get("candidate_id")
                and item.get("candidate_path")
            ]
            if isinstance(raw_bundles, list)
            else []
        )
        cards = self._supported_cards(context)
        destination = context.run_dir / "research" / "strategy_transfer.json"
        if not bundles:
            return PhaseResult(
                status="skipped",
                artifacts={
                    "strategy_transfer": write_json(
                        destination,
                        {
                            "schema": STRATEGY_TRANSFER_SCHEMA,
                            "status": "skipped",
                            "reason": "no island bundle has a valid candidate",
                            "islands": [],
                        },
                    )
                },
            )

        transfer_root = context.run_dir / "research" / "transfer"
        island_results: list[dict[str, Any]] = []
        attempted = 0
        for bundle in sorted(bundles, key=lambda item: int(item["island"])):
            island = int(bundle["island"])
            base_id = str(bundle["candidate_id"])
            base_path = Path(str(bundle["candidate_path"]))
            island_dir = transfer_root / f"island-{island}"
            donors = [card for card in cards if int(card["source_island"]) != island][
                : self.max_strategies_per_island
            ]
            if not donors:
                winner_solver = island_dir / "winner_solver"
                copy_solver_tree(base_path, winner_solver)
                island_results.append(
                    {
                        "island": island,
                        "base_candidate_id": base_id,
                        "donor_strategy_ids": [],
                        "status": "skipped",
                        "reason": "no controller-supported strategy from another island",
                        "base": {"candidate_id": base_id, "tree_hash": tree_hash(base_path),
                                 "validation": _validation_evidence(context.run_dir, base_id),
                                 "retained": True},
                        "winner_candidate_id": base_id,
                        "winner_solver": str(winner_solver),
                        "attempts": [],
                    }
                )
                continue

            donor_path = write_json(
                island_dir / "transfer_strategies.json",
                {
                    "schema": "transfer_strategy_cards/1",
                    "target_island": island,
                    "strategies": donors,
                    "interpretation": (
                        "These are hypotheses supported by controller-run ablations on "
                        "other islands. Compatibility with this solver remains unproven."
                    ),
                },
            )
            handles: dict[str, tuple[VariantHandle, float]] = {}
            attempts: list[dict[str, Any]] = []
            for attempt_index in range(self.attempts_per_island):
                attempted += 1
                variant_id = f"transfer-i{island:02d}-a{attempt_index:02d}"
                entry: dict[str, Any] = {
                    "variant_id": variant_id,
                    "status": "running",
                }
                try:
                    result = context.services.run_trusted_agent(
                        SolverJob(
                            role="strategy-integrator",
                            job_id=variant_id,
                            prompt=_strategy_transfer_prompt(
                                island=island,
                                base_candidate=base_id,
                                strategy_ids=[str(card["strategy_id"]) for card in donors],
                                prompt_version=self.prompt_version,
                            ),
                            inputs={
                                "candidate": base_path,
                                "transfer_strategies.json": donor_path,
                            },
                            expected_outputs=("candidate", "transfer_manifest.json"),
                            recover_outputs_on_nonzero_exit=True,
                            worker_index=self.worker_index,
                            tools=self.tools,
                            timeout_seconds=self.timeout_seconds,
                            token_budget=self.token_budget,
                            max_budget_usd=self.max_budget_usd,
                            trace_links={
                                "island": island,
                                "candidate_id": variant_id,
                                "parent_id": base_id,
                            },
                        )
                    )
                    manifest = _normalize_transfer_manifest(
                        read_json_object(result.outputs["transfer_manifest.json"]),
                        allowed={str(card["strategy_id"]) for card in donors},
                    )
                    explanation = capture_solver_explanation(
                        result.outputs["candidate"],
                        context.run_dir / "explanations" / f"{variant_id}.json",
                        attempt_id=variant_id,
                    )
                    handle = context.services.materialize_variant(
                        VariantRequest(
                            variant_id=variant_id,
                            base=base_path,
                            tree=result.outputs["candidate"],
                            expected_base_hash=tree_hash(base_path),
                        )
                    )
                    public = context.services.evaluate_public_variant(
                        handle,
                        f"{variant_id}-public",
                    )
                    if not public.success:
                        raise RuntimeError(public.error or "public evaluation failed")
                    handles[variant_id] = (handle, public.score)
                    entry.update(
                        {
                            "status": "succeeded",
                            "public_score": public.score,
                            "tree_hash": handle.tree_hash,
                            "manifest": manifest,
                            "solver_explanation": str(explanation),
                        }
                    )
                except Exception as exc:
                    entry.update(
                        {
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                attempts.append(entry)

            candidate_ids = [base_id, *handles]
            selected = context.services.select_by_validation(candidate_ids, 1)
            winner = selected[0] if selected else base_id
            # "succeeded" above only means a variant was built and scored; say which
            # tree the island actually kept and on what controller-only evidence.
            for entry in attempts:
                if entry["variant_id"] in handles:
                    entry["validation"] = _validation_evidence(context.run_dir, entry["variant_id"])
                entry["retained"] = entry["variant_id"] == winner
            base_record = {
                "candidate_id": base_id,
                "tree_hash": tree_hash(base_path),
                "validation": _validation_evidence(context.run_dir, base_id),
                "retained": winner == base_id,
            }
            winner_path = base_path
            if winner in handles:
                handle, public_score = handles[winner]
                winner_path = handle.path
                winner = context.services.register_finalist(
                    handle,
                    {
                        "island": island,
                        "parent_id": base_id,
                        "public_score": public_score,
                        "source": "strategy_transfer",
                    },
                )
            winner_solver = island_dir / "winner_solver"
            copy_solver_tree(winner_path, winner_solver)
            island_results.append(
                {
                    "island": island,
                    "base_candidate_id": base_id,
                    "donor_strategy_ids": [str(card["strategy_id"]) for card in donors],
                    "status": "succeeded" if handles else "no_valid_transfer",
                    "base": base_record,
                    "selection": {"policy": "controller_validation_top1",
                                  "problem_set": "validation", "candidates": candidate_ids},
                    "winner_candidate_id": winner,
                    "winner_solver": str(winner_solver),
                    "attempts": attempts,
                    "validation_selection_calls": 1,
                }
            )
            context.emit(
                "island_strategy_transfer_finished",
                "succeeded" if handles else "failed",
                {
                    "island": island,
                    "base_candidate_id": base_id,
                    "winner_candidate_id": winner,
                    "attempt_count": len(attempts),
                },
            )

        output = write_json(
            destination,
            {
                "schema": STRATEGY_TRANSFER_SCHEMA,
                "status": "ok" if attempted else "skipped",
                "transfer_unit": "controller_verified_semantic_strategy_card",
                "whole_solver_migration": False,
                "islands": island_results,
            },
        )
        return PhaseResult(
            status="succeeded" if attempted else "skipped",
            artifacts={"strategy_transfer": output},
        )

    def _supported_cards(self, context: PhaseContext) -> list[dict[str, Any]]:
        supported: list[dict[str, Any]] = []
        for _path, island, card in supported_strategy_cards(context.artifacts["strategy_cards"]):
            supported.append(
                {
                    "strategy_id": str(card["strategy_id"]),
                    "source_island": island,
                    "claim": str(card.get("claim", ""))[:4000],
                    "mechanism": str(card.get("mechanism", ""))[:4000],
                    "pseudocode": list(card.get("pseudocode", []))[:30],
                    "applicability": list(card.get("applicability", []))[:20],
                    "risks": list(card.get("risks", []))[:20],
                    "measured_effect": card["ablation"].get("mean_effect_when_present"),
                    "evaluation_mode": card["ablation"].get("evaluation_mode", "public"),
                }
            )
        return sorted(
            supported,
            key=lambda item: (
                -float(item["measured_effect"] or 0.0),
                int(item["source_island"]),
                str(item["strategy_id"]),
            ),
        )


def _strategy_transfer_prompt(
    *,
    island: int,
    base_candidate: str,
    strategy_ids: Sequence[str],
    prompt_version: str,
) -> str:
    return f"""ROLE: strategy-integrator
PROMPT_TEMPLATE: {prompt_version}

Improve target island {island} candidate {base_candidate} in candidate/. The controller has
provided these strategies, each supported by a leave-one-out ablation on another island:
{list(strategy_ids)!r}

Read transfer_strategies.json and the target solver. Treat every donor strategy as a
semantic hypothesis, not as source code to copy. Preserve the target island's coherent
algorithm and integrate only strategies that are compatible. Do not replace candidate/
with another island's solver and do not seek validation or hidden data. You have no
evaluation broker in this role; the controller performs public, integrity, and validation
checks after you finish.

Leave the complete edited solver in candidate/. Write transfer_manifest.json at the
workspace root, alongside candidate/, not inside candidate/. Use this JSON object:
{{"schema":"strategy_transfer_manifest/1","attempted_strategy_ids":["..."],
"applied_strategy_ids":["..."],"rejected_strategy_ids":["..."],
"summary":"..."}}
Before finishing, verify that this root-level file exists and parse it as JSON.
Check the edited Python files for syntax errors without running any evaluation.

{explanation_instructions("candidate")}
Explain which transferred mechanisms remain and their code lines.
"""


def _normalize_transfer_manifest(
    payload: Mapping[str, Any],
    *,
    allowed: set[str],
) -> dict[str, Any]:
    if payload.get("schema") != "strategy_transfer_manifest/1":
        raise ValueError("transfer manifest must use strategy_transfer_manifest/1")

    def ids(name: str) -> list[str]:
        value = payload.get(name, [])
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"transfer manifest {name} must be a list")
        normalized = [str(item) for item in value]
        unknown = sorted(set(normalized).difference(allowed))
        if unknown:
            raise ValueError(f"transfer manifest names unknown strategies: {unknown!r}")
        return list(dict.fromkeys(normalized))

    attempted = ids("attempted_strategy_ids")
    applied = ids("applied_strategy_ids")
    rejected = ids("rejected_strategy_ids")
    if set(applied).intersection(rejected):
        raise ValueError("a transferred strategy cannot be both applied and rejected")
    if not set(applied).union(rejected).issubset(attempted):
        raise ValueError("applied and rejected strategies must be declared attempted")
    return {
        "schema": "strategy_transfer_manifest/1",
        "attempted_strategy_ids": attempted,
        "applied_strategy_ids": applied,
        "rejected_strategy_ids": rejected,
        "summary": str(payload.get("summary", ""))[:4000],
    }


def _validation_evidence(run_dir: Path, candidate_id: str) -> dict[str, Any]:
    """Controller-only validation outcome for one tree; ``None`` fields mean not assessed."""
    assessment = stage_assessment(run_dir, candidate_id, "validation") or {}
    results = assessment.get("results", {})
    review = results.get("integrity") or {}
    return {
        "assessed_tree_hash": assessment.get("tree_hash"),
        "performance_score": (results.get("performance") or {}).get("score"),
        "assessment_score": assessment.get("total"),
        "accepted": assessment.get("accepted"),
        "review_decision": {key: review.get(key) for key in ("status", "score", "error")},
    }
