"""Evaluate research variants and public challenger comparisons."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from optiprofiler_evolve.agents.jobs import safe_identifier as _safe_identifier
from optiprofiler_evolve.controller.contracts import VariantHandle
from optiprofiler_evolve.criteria.performance.references import materialize_reference
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.solver import tree_hash

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.engine import EvolutionEngine


class ResearchEvaluator:
    def __init__(self, engine: EvolutionEngine) -> None:
        self.engine = engine

    def evaluate(
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
            handle.path,
            evaluation_mode,
            output_dir,
            candidate_id=handle.variant_id,
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
