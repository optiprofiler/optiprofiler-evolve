"""The direction scout workflow stage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from optiprofiler_evolve.agents.contracts import AgentJob
from optiprofiler_evolve.files import read_json_object, write_json
from optiprofiler_evolve.phases._research import (
    DIRECTION_SCHEMA,
    _prepared,
    _tool_config,
    _tool_payload,
    normalize_direction_cards,
)
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult


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
        island_count = prepared.islands
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
                            "seed": prepared.seed,
                            "public_evidence": prepared.seed_public_evidence,
                            "solver_contract.json": prepared.solver_contract,
                        },
                        expected_outputs=("direction_cards.json",),
                        worker_index=self.worker_index,
                        tools=self.tools,
                        timeout_seconds=self.timeout_seconds,
                        token_budget=self.token_budget,
                        max_budget_usd=self.max_budget_usd,
                        trace_links={"island": job_island} if job_island is not None else {},
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
        payload["status"] = (
            "off-fallback" if failed and not all_cards else "partial" if failed else "ok"
        )
        write_json(destination, payload)
        context.emit(
            "directions_ready",
            "succeeded" if all_cards else "skipped",
            {"mode": self.mode, "card_count": len(all_cards), "status": payload["status"]},
        )
        return PhaseResult(artifacts={"directions": destination})


def _direction_prompt(*, max_directions: int, prompt_version: str, island: int | None) -> str:
    scope = "the shared island portfolio" if island is None else f"island {island}"
    return f"""ROLE: direction-scout
PROMPT_TEMPLATE: {prompt_version}

Read the solver under seed/, its public benchmark evidence, and solver_contract.json.
Research up to {max_directions} genuinely different DFO algorithm directions for {scope}.
You are read-only with respect to the seed. Do not implement or evaluate a solver. Use web
search only for public literature and document every claim with a URL.

Every proposed tactic must respect the runtime, editable scope, and forbidden candidate
imports in solver_contract.json. Recommend mechanisms that can be implemented directly
inside the candidate rather than delegating to an unavailable or prohibited optimizer.

Create a valid draft direction_cards.json before broad reading or web research, then refine
it as evidence permits. Read artifact_index.json, feedback.md, reports, and selected plots;
do not inspect copied benchmark implementation files. Preserve a valid output even if the
remaining tool or model budget is exhausted.

Write direction_cards.json with this shape:
{{"cards":[{{"card_id":"d1","title":"...","hypothesis":"...",
"tactics":["..."],"citations":[{{"url":"...","claim":"..."}}]}}]}}
Keep hypotheses independent enough to support an island ablation. Do not claim that a
direction works; state what observable public behavior would support or refute it.
"""
