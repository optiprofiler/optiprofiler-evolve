"""Read-only exploration inputs and controller-owned commit operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from random import Random
from typing import TYPE_CHECKING

from optiprofiler_evolve.shared.config import EvolutionConfig, WorkerConfig, WorkersConfig
from optiprofiler_evolve.shared.models import CandidateRecord
from optiprofiler_evolve.shared.protocols import AfterIterationPolicy, PhaseContext, PopulationEdit

if TYPE_CHECKING:
    from optiprofiler_evolve.shared.models import Attempt


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
