"""Bind core phases to narrow controller capabilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from functools import partial

from optiprofiler_evolve.controller.runtime import RunEnvironment
from optiprofiler_evolve.phases.contracts import PhaseContext
from optiprofiler_evolve.phases.explore import ExploreContext, ExplorePhase
from optiprofiler_evolve.phases.hidden import HiddenContext, HiddenPhase
from optiprofiler_evolve.phases.prepare import PrepareContext, PreparePhase
from optiprofiler_evolve.phases.report import ReportContext, ReportPhase
from optiprofiler_evolve.phases.validate import ValidateContext, ValidatePhase


def prepare_context(bindings, base: PhaseContext) -> PhaseContext:
    environment = bindings.environment
    common = {field.name: getattr(base, field.name) for field in fields(base)}
    return PrepareContext(
        **common,
        initial=environment.initial,
        interface=environment.interface,
        runtime=environment.runtime,
        editable=environment.editable,
        config=environment.config,
        evaluator_factory=environment.evaluator_factory,
        validate_candidate=environment.validate_candidate,
        review_candidate=bindings.review_candidate,
    )


def explore_context(bindings, base: PhaseContext) -> PhaseContext:
    environment = bindings.environment
    common = {field.name: getattr(base, field.name) for field in fields(base)}
    if environment.state.evaluator is None or environment.state.data is None:
        raise RuntimeError("Explore phase requires prepared evaluator and data.")
    return ExploreContext(
        **common,
        evolution=environment.config.evolution,
        workers=environment.config.workers,
        after_iteration=bindings.after_iteration,
        populations=environment.commits.population_snapshot,
        set_directions=environment.commits.set_directions,
        select_parent=environment.commits.select_parent,
        select_worker=bindings.select_worker,
        run_attempt=bindings.run_attempt,
        failed_attempt=bindings.failed_attempt,
        accept_attempt=environment.commits.accept_and_record,
        trim_population=environment.commits.trim_population,
        apply_population_edit=environment.commits.apply_population_edit,
        write_checkpoint=environment.commits.write_checkpoint,
        raise_if_cancelled=bindings.raise_if_cancelled,
        emit_scoped=environment.emit,
        refresh_status=bindings.refresh,
        refresh_status_if_due=bindings.refresh_if_due,
    )


def validate_context(bindings, base: PhaseContext) -> PhaseContext:
    environment = bindings.environment
    common = {field.name: getattr(base, field.name) for field in fields(base)}
    return ValidateContext(
        **common,
        populations=environment.commits.population_snapshot(),
        research_finalists=tuple(environment.state.research_finalists.values()),
        fixed_finalists=environment.commits.fixed_finalists,
    )


def hidden_context(bindings, base: PhaseContext) -> PhaseContext:
    environment = bindings.environment
    common = {field.name: getattr(base, field.name) for field in fields(base)}
    if (
        environment.state.champion is None
        or environment.state.evaluator is None
    ):
        raise RuntimeError("Hidden phase requires a validation-selected champion.")
    return HiddenContext(
        **common,
        champion=environment.state.champion,
        evaluator=environment.state.evaluator,
        begin_hidden=environment.commits.begin_hidden,
    )


def report_context(bindings, base: PhaseContext) -> PhaseContext:
    environment = bindings.environment
    common = {field.name: getattr(base, field.name) for field in fields(base)}
    if environment.state.final_result is None or environment.state.final_solver is None:
        raise RuntimeError("Report phase requires a hidden evaluation result.")
    return ReportContext(
        **common,
        final_result=environment.state.final_result,
        final_solver=environment.state.final_solver,
    )


CONTEXT_FACTORIES = {
    PreparePhase: prepare_context,
    ExplorePhase: explore_context,
    ValidatePhase: validate_context,
    HiddenPhase: hidden_context,
    ReportPhase: report_context,
}


@dataclass(frozen=True)
class CorePhaseBindings:
    environment: RunEnvironment
    review_candidate: Callable
    run_attempt: Callable
    failed_attempt: Callable
    select_worker: Callable
    after_iteration: tuple
    refresh: Callable
    refresh_if_due: Callable
    raise_if_cancelled: Callable


def context_factories(environment: RunEnvironment, **capabilities):
    bindings = CorePhaseBindings(environment, **capabilities)
    return {name: partial(factory, bindings) for name, factory in CONTEXT_FACTORIES.items()}
