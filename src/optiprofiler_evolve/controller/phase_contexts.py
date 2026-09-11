"""Bind core phases to narrow controller capabilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from functools import partial

from optiprofiler_evolve.controller.assessment import CandidateAssessor
from optiprofiler_evolve.controller.runtime import RunEnvironment
from optiprofiler_evolve.phases.attempts.contracts import AfterIterationPolicy
from optiprofiler_evolve.phases.contracts import PhaseContext
from optiprofiler_evolve.phases.explore import ExploreContext, ExplorePhase
from optiprofiler_evolve.phases.hidden import HiddenContext, HiddenPhase
from optiprofiler_evolve.phases.prepare import PrepareContext, PreparePhase
from optiprofiler_evolve.phases.report import ReportContext, ReportPhase
from optiprofiler_evolve.phases.validate import ValidateContext, ValidatePhase


def context_factories(
    environment: RunEnvironment, *, review_candidate: Callable, run_attempt: Callable,
    failed_attempt: Callable, select_worker: Callable,
    after_iteration: tuple[AfterIterationPolicy, ...], refresh: Callable,
    refresh_if_due: Callable, raise_if_cancelled: Callable,
) -> dict[type, Callable[[PhaseContext], PhaseContext]]:
    def prepare_context(base: PhaseContext) -> PrepareContext:
        return PrepareContext(
            **_common_inputs(base),
            initial=environment.initial,
            interface=environment.interface,
            runtime=environment.runtime,
            editable=environment.editable,
            config=environment.config,
            create_assessor=partial(
                CandidateAssessor,
                config=environment.config, runtime=environment.runtime,
                interface=environment.interface, run_dir=environment.run_dir,
                evaluator_factory=environment.evaluator_factory,
                criterion_factories=environment.criterion_factories,
                review=review_candidate,
            ),
            validate_candidate=environment.validate_candidate,
        )

    def explore_context(base: PhaseContext) -> ExploreContext:
        if environment.state.evaluator is None or environment.state.data is None:
            raise RuntimeError("Explore phase requires prepared evaluator and data.")
        return ExploreContext(
            **_common_inputs(base),
            evolution=environment.config.evolution,
            workers=environment.config.workers,
            after_iteration=after_iteration,
            populations=environment.commits.population_snapshot,
            set_directions=environment.commits.set_directions,
            select_parent=environment.commits.select_parent,
            select_worker=select_worker,
            run_attempt=run_attempt,
            failed_attempt=failed_attempt,
            accept_attempt=environment.commits.accept_and_record,
            trim_population=environment.commits.trim_population,
            apply_population_edit=environment.commits.apply_population_edit,
            write_checkpoint=environment.commits.write_checkpoint,
            raise_if_cancelled=raise_if_cancelled,
            emit_scoped=environment.emit,
            refresh_status=refresh,
            refresh_status_if_due=refresh_if_due,
        )

    def validate_context(base: PhaseContext) -> ValidateContext:
        return ValidateContext(
            **_common_inputs(base),
            populations=environment.commits.population_snapshot(),
            research_finalists=tuple(environment.state.research_finalists.values()),
            fixed_finalists=environment.commits.fixed_finalists,
        )

    def hidden_context(base: PhaseContext) -> HiddenContext:
        champion = environment.state.champion
        evaluator = environment.state.evaluator
        if champion is None or evaluator is None:
            raise RuntimeError("Hidden phase requires a validation-selected champion.")
        return HiddenContext(
            **_common_inputs(base), champion=champion, evaluator=evaluator,
            begin_hidden=environment.commits.begin_hidden,
        )

    def report_context(base: PhaseContext) -> ReportContext:
        final_result = environment.state.final_result
        final_solver = environment.state.final_solver
        if final_result is None or final_solver is None:
            raise RuntimeError("Report phase requires a hidden evaluation result.")
        return ReportContext(
            **_common_inputs(base), final_result=final_result, final_solver=final_solver,
        )

    return {
        PreparePhase: prepare_context,
        ExplorePhase: explore_context,
        ValidatePhase: validate_context,
        HiddenPhase: hidden_context,
        ReportPhase: report_context,
    }


def _common_inputs(base: PhaseContext) -> dict[str, object]:
    return {field.name: getattr(base, field.name) for field in fields(PhaseContext)}
