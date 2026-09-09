"""Bind core phases to narrow controller capabilities."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from optiprofiler_evolve.phases.contracts import PhaseContext
from optiprofiler_evolve.phases.explore import ExploreContext
from optiprofiler_evolve.phases.hidden import HiddenContext
from optiprofiler_evolve.phases.prepare import PrepareContext
from optiprofiler_evolve.phases.report import ReportContext
from optiprofiler_evolve.phases.validate import ValidateContext

if TYPE_CHECKING:
    from optiprofiler_evolve.controller.engine import EvolutionEngine


def bind_context(engine: EvolutionEngine, name: str, base: PhaseContext) -> PhaseContext:
    """Bind the fixed core capabilities; custom phases retain the public base context."""
    common = {field.name: getattr(base, field.name) for field in fields(base)}
    if name == "prepare":
        return PrepareContext(
            **common,
            initial=engine.initial,
            interface=engine.interface,
            runtime=engine.runtime,
            editable=engine.editable,
            config=engine.config,
            evaluator_factory=engine.evaluator_factory,
            validate_candidate=engine._validate_candidate,
            review_candidate=engine.integrity_executor.review,
        )
    if name == "explore":
        if engine.evaluator is None or engine.data is None:
            raise RuntimeError("Explore phase requires prepared evaluator and data.")
        return ExploreContext(
            **common,
            evolution=engine.config.evolution,
            workers=engine.config.workers,
            after_iteration=engine.after_iteration,
            populations=engine._population_snapshot,
            set_directions=engine._set_directions,
            select_parent=engine._select_parent,
            select_worker=engine._select_worker,
            run_attempt=engine.attempt_runner.run,
            failed_attempt=engine.attempt_runner.failed,
            accept_attempt=engine._accept_and_record,
            trim_population=engine._trim_population,
            apply_population_edit=engine._apply_population_edit,
            write_checkpoint=engine._write_checkpoint,
            raise_if_cancelled=engine._raise_if_cancelled,
            emit_scoped=engine._emit,
            refresh_status=engine.views.refresh,
            refresh_status_if_due=engine.views.refresh_if_due,
        )
    if name == "validate":
        return ValidateContext(
            **common,
            populations=engine._population_snapshot(),
            research_finalists=tuple(engine.research_finalists.values()),
            fixed_finalists=engine._fixed_finalists,
        )
    if name == "hidden":
        if engine.champion is None or engine.evaluator is None or engine.data is None:
            raise RuntimeError("Hidden phase requires a validation-selected champion.")
        return HiddenContext(
            **common,
            champion=engine.champion,
            evaluator=engine.evaluator,
            data=engine.data,
        )
    if name == "report":
        if engine.final_result is None or engine.final_solver is None:
            raise RuntimeError("Report phase requires a hidden evaluation result.")
        return ReportContext(
            **common,
            final_result=engine.final_result,
            final_solver=engine.final_solver,
        )
    return base
