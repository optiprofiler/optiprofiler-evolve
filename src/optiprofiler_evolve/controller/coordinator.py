"""Execute declared phases and commit successful staged results."""

from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

from optiprofiler_evolve.config import ComponentConfig
from optiprofiler_evolve.controller.commits import RunCommits
from optiprofiler_evolve.controller.contracts import ControllerServices
from optiprofiler_evolve.controller.state import RunState
from optiprofiler_evolve.errors import RunCancelled
from optiprofiler_evolve.phases.contracts import Phase, PhaseContext, phase_optional_requires


class RunCoordinator:
    def __init__(
        self,
        *,
        state: RunState,
        phases: tuple[tuple[ComponentConfig, Phase], ...],
        run_dir: Path,
        services: ControllerServices,
        commits: RunCommits,
        contexts: Mapping[type, Callable[[PhaseContext], PhaseContext]],
        emit: Callable,
        refresh: Callable,
        raise_if_cancelled: Callable,
    ) -> None:
        self.state = state
        self.phases = phases
        self.run_dir = run_dir
        self.services = services
        self.commits = commits
        self.contexts = dict(contexts)
        self.emit = emit
        self.refresh = refresh
        self.raise_if_cancelled = raise_if_cancelled

    def run(self) -> None:
        for spec, phase in self.phases:
            self.run_phase(phase, spec.options)

    def run_phase(self, phase: Phase, options: Mapping[str, object]) -> Mapping[str, object]:
        """Execute one stage of the prevalidated workflow and verify its outputs."""
        self.raise_if_cancelled()
        scope = {"phase": phase.name}
        self.emit("phase_started", "running", scope=scope)
        self.refresh()
        try:
            inputs = phase.requires.union(phase_optional_requires(phase))
            artifacts = self.state.artifacts
            context = PhaseContext(
                run_dir=self.run_dir,
                options=MappingProxyType(dict(options)),
                artifacts=MappingProxyType(
                    {
                        key: artifacts[key]
                        for key in inputs
                        if key in artifacts
                    }
                ),
                emit=lambda kind, status, data=None: self.emit(
                    kind, status, scope=scope, data=data
                ),
                services=self.services,
            )
            # Capabilities follow registered implementations, never owner-selected names.
            factory = next(
                (self.contexts[cls] for cls in type(phase).__mro__ if cls in self.contexts),
                None,
            )
            result = phase.run(factory(context) if factory is not None else context)
            if result.status == "cancelled":
                raise RunCancelled(f"Phase {phase.name!r} was cancelled.")
            if result.status == "failed":
                raise RuntimeError(f"Phase {phase.name!r} failed: {result.error or 'no reason supplied'}")
            undeclared = set(result.artifacts).difference(phase.provides)
            if undeclared:
                raise RuntimeError(
                    f"Phase {phase.name!r} produced undeclared artifacts {sorted(undeclared)!r}."
                )
            missing = phase.provides.difference(result.artifacts)
            if missing:
                raise RuntimeError(
                    f"Phase {phase.name!r} omitted declared artifacts {sorted(missing)!r}."
                )
            self.commits.commit_phase_result(result)
            self.emit(
                "phase_finished",
                result.status,
                scope=scope,
                data={"artifacts": sorted(result.artifacts)},
            )
        except RunCancelled:
            self.emit("phase_finished", "cancelled", scope=scope)
            raise
        except Exception as exc:
            self.emit(
                "phase_finished",
                "failed",
                scope=scope,
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        finally:
            self.refresh()
        self.raise_if_cancelled()
        return result.artifacts
