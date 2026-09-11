"""Explore stage and its typed inputs/results."""

from __future__ import annotations

import concurrent.futures
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from random import Random

from optiprofiler_evolve.config import EvolutionConfig, EvolveConfig, WorkerConfig, WorkersConfig
from optiprofiler_evolve.errors import RunCancelled as _RunCancelled
from optiprofiler_evolve.files import read_json_object
from optiprofiler_evolve.models import Attempt, CandidateRecord
from optiprofiler_evolve.observability.provenance import coordinate_seed
from optiprofiler_evolve.phases.attempts.contracts import (
    AfterIterationPolicy,
    IterationView,
    PopulationEdit,
    validate_attempt_steps,
)
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult


@dataclass(frozen=True)
class ExploreContext(PhaseContext):
    evolution: EvolutionConfig
    workers: WorkersConfig
    after_iteration: tuple[AfterIterationPolicy, ...]
    populations: Callable[[], tuple[tuple[CandidateRecord, ...], ...]]
    set_directions: Callable[[Mapping[int, dict[str, object]]], None]
    select_parent: Callable[[Sequence[CandidateRecord], Random], CandidateRecord]
    select_worker: Callable[[Random], WorkerConfig]
    run_attempt: Callable[..., Attempt]
    failed_attempt: Callable[..., Attempt]
    accept_attempt: Callable[[Attempt], None]
    trim_population: Callable[[Sequence[CandidateRecord]], list[CandidateRecord]]
    apply_population_edit: Callable[[PopulationEdit], None]
    write_checkpoint: Callable[[int], None]
    raise_if_cancelled: Callable[[], None]
    emit_scoped: Callable[..., None]
    refresh_status: Callable[[], None]
    refresh_status_if_due: Callable[[], None]


class ExplorePhase:
    """Execute the explore stage through its bounded controller context."""

    name = "explore"
    requires = frozenset(["prepared"])
    optional_requires = frozenset({"directions"})
    provides = frozenset(["populations"])

    def validate_config(self, config: EvolveConfig) -> None:
        validate_attempt_steps(item.name for item in config.workflow.attempt_steps)

    def run(self, context: ExploreContext) -> PhaseResult:
        context.set_directions(_direction_assignments(context.artifacts.get("directions")))
        should_stop = False
        for iteration in range(1, context.evolution.iterations + 1):
            context.raise_if_cancelled()
            iteration_scope = {"phase": "explore", "iteration": iteration}
            context.emit_scoped("iteration_started", "running", scope=iteration_scope)
            context.refresh_status()
            jobs: list[tuple[int, int, CandidateRecord, WorkerConfig]] = []
            for island, population in enumerate(context.populations()):
                for attempt_index in range(context.evolution.attempts_per_island):
                    parent_rng = random.Random(
                        coordinate_seed(
                            context.evolution.random_seed,
                            "explore",
                            iteration,
                            island,
                            attempt_index,
                            "parent",
                        )
                    )
                    worker_rng = random.Random(
                        coordinate_seed(
                            context.evolution.random_seed,
                            "explore",
                            iteration,
                            island,
                            attempt_index,
                            "worker",
                        )
                    )
                    jobs.append(
                        (
                            island,
                            attempt_index,
                            context.select_parent(population, parent_rng),
                            context.select_worker(worker_rng),
                        )
                    )

            attempts: list[Attempt] = []
            max_workers = min(context.workers.max_parallel, len(jobs))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        context.run_attempt,
                        iteration=iteration,
                        island=island,
                        attempt_index=attempt_index,
                        parent=parent,
                        worker=worker,
                    ): (island, attempt_index, parent, worker)
                    for island, attempt_index, parent, worker in jobs
                }
                for future in concurrent.futures.as_completed(futures):
                    island, attempt_index, parent, worker = futures[future]
                    try:
                        attempts.append(future.result())
                    except _RunCancelled:
                        raise
                    except Exception as exc:
                        attempts.append(
                            context.failed_attempt(
                                iteration,
                                island,
                                attempt_index,
                                parent,
                                worker,
                                exc,
                            )
                        )
                    context.refresh_status_if_due()

            context.raise_if_cancelled()
            # Explicit iteration barrier: only the engine accepts and records.
            for attempt in sorted(
                attempts,
                key=lambda item: (item.record.island, item.record.attempt_index),
            ):
                context.accept_attempt(attempt)

            view = IterationView(
                iteration=iteration,
                populations=tuple(
                    tuple(context.trim_population(pop)) for pop in context.populations()
                ),
                attempt_ids=tuple(item.record.attempt_id for item in attempts),
                rng_seed=coordinate_seed(
                    context.evolution.random_seed, "explore", iteration, "after_iteration"
                ),
            )
            for index, policy in enumerate(context.after_iteration):
                policy_scope = {
                    "phase": "explore",
                    "iteration": iteration,
                    "step": policy.name,
                    "step_idx": index,
                }
                context.emit_scoped("policy_started", "running", scope=policy_scope)
                try:
                    edit = policy.propose(view)
                    context.apply_population_edit(edit)
                except Exception as exc:
                    context.emit_scoped(
                        "policy_finished",
                        "failed",
                        scope=policy_scope,
                        data={"error": f"{type(exc).__name__}: {exc}"},
                    )
                    raise
                else:
                    context.emit_scoped(
                        "policy_finished",
                        "succeeded",
                        scope=policy_scope,
                        data={
                            "kill": list(edit.kill),
                            "migrate": [list(item) for item in edit.migrate],
                            "budget": dict(edit.budget),
                            "stop": edit.stop,
                            "reason": edit.reason,
                        },
                    )
                should_stop = should_stop or edit.stop
            context.write_checkpoint(iteration)
            context.emit_scoped(
                "iteration_finished",
                "succeeded",
                scope=iteration_scope,
                data={"stop": should_stop, "attempt_count": len(attempts)},
            )
            context.refresh_status()
            if should_stop:
                break
        return PhaseResult(artifacts={"populations": context.populations()})


def _direction_assignments(artifact: object) -> dict[int, dict[str, object]]:
    if artifact is None:
        return {}
    path = Path(str(artifact))
    payload = read_json_object(path)
    cards = {
        str(card.get("card_id")): dict(card)
        for card in payload.get("cards", [])
        if isinstance(card, Mapping) and card.get("card_id")
    }
    assignments: dict[int, dict[str, object]] = {}
    raw_assignment = payload.get("assignment", {})
    if not isinstance(raw_assignment, Mapping):
        raise ValueError("Direction assignments must be a mapping.")
    for island, card_id in raw_assignment.items():
        if card_id is None:
            continue
        if str(card_id) not in cards:
            raise ValueError(f"Direction assignment references missing card {card_id!r}.")
        assignments[int(island)] = cards[str(card_id)]
    return assignments
