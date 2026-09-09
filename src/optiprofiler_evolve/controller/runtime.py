"""Controller-side inputs and execution ports, independent of the application."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from optiprofiler_evolve.config import EvolveConfig
from optiprofiler_evolve.controller.commits import RunCommits
from optiprofiler_evolve.controller.state import RunState
from optiprofiler_evolve.solver import InterfaceSpec


@dataclass(frozen=True, slots=True)
class RunEnvironment:
    initial: Path
    interface: InterfaceSpec
    runtime: str
    editable: tuple[str, ...]
    config: EvolveConfig
    run_dir: Path
    state: RunState
    commits: RunCommits
    attempt_steps: tuple
    evaluator_factory: Callable | None
    emit: Callable
    scoped_emitter: Callable
    validate_candidate: Callable[[Path], None]
    memory_for: Callable[[int], str | None]
    llm_client: Callable
    cancellation_event: Event
