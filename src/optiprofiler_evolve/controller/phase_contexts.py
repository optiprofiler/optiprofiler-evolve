"""Bind core phases to narrow inputs; commit their results in the controller."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from optiprofiler_evolve.phases.explore.context import ExploreContext
from optiprofiler_evolve.phases.hidden.context import HiddenContext, HiddenResult
from optiprofiler_evolve.phases.prepare.context import PrepareContext, PrepareResult
from optiprofiler_evolve.phases.report.context import ReportContext, ReportResult
from optiprofiler_evolve.phases.validate.context import ValidateContext, ValidateResult
from optiprofiler_evolve.shared.protocols import PhaseContext, PhaseResult

if TYPE_CHECKING:
    from .engine import EvolutionEngine


def bind_context(engine: EvolutionEngine, name: str, base: PhaseContext) -> PhaseContext:
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
            refresh_status=engine._refresh_status,
            refresh_status_if_due=engine._refresh_status_if_due,
        )
    if name == "validate":
        return ValidateContext(
            **common,
            populations=engine._population_snapshot(),
            research_finalists=tuple(engine.research_finalists.values()),
            fixed_finalists=engine._fixed_finalists,
            assess_complexity=engine._assess_complexity,
        )
    if name == "hidden":
        if engine.champion is None or engine.evaluator is None or engine.data is None:
            raise RuntimeError("Hidden phase requires a validation-selected champion.")
        return HiddenContext(
            **common,
            champion=engine.champion,
            evaluator=engine.evaluator,
            data=engine.data,
            check_complexity=engine._check_complexity,
        )
    if name == "report":
        if engine.final_result is None or engine.final_solver is None:
            raise RuntimeError("Report phase requires a hidden evaluation result.")
        return ReportContext(
            **common,
            final_result=engine.final_result,
            final_solver=engine.final_solver,
            check_complexity=engine._check_complexity,
        )
    return base


def commit_result(engine: EvolutionEngine, result: PhaseResult) -> None:
    if isinstance(result, PrepareResult):
        engine.data = result.data
        engine.reference = result.reference
        engine.evaluator = result.evaluator
        engine.complexity = result.complexity
        engine.candidates[result.seed.candidate_id] = result.seed
        engine.populations = [[result.seed] for _ in range(engine.config.evolution.islands)]
        engine._write_checkpoint(0)
    elif isinstance(result, ValidateResult):
        engine.champion = result.champion
    elif isinstance(result, HiddenResult):
        engine.final_result = result.final_result
        engine.final_solver = result.final_solver
    elif isinstance(result, ReportResult):
        engine.result = result.result
