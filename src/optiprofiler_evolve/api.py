"""The single public entrypoint."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import EvolveConfig, load_config
from .engine import EvolutionEngine
from .models import EvolveResult
from .solver import InterfaceSpec


def evolve(
    initial: str | Path,
    *,
    interface: str = "solver.py:solver",
    runtime: str = "auto",
    editable: Sequence[str] = (".",),
    config: EvolveConfig | Mapping[str, Any] | str | Path,
    run_dir: str | Path | None = None,
) -> EvolveResult:
    """Evolve one solver against one fixed OptiProfiler problem-library experiment.

    Parameters
    ----------
    initial:
        A solver file or repository. It is copied and never edited in place.
    interface:
        Entrypoint in ``relative/file.py:function`` form.
    runtime:
        ``"auto"`` infers Python or MATLAB from the interface suffix.
    editable:
        Relative paths or globs that workers may change.
    config:
        Strict experiment mapping, :class:`EvolveConfig`, or YAML file.
    run_dir:
        New directory for all state and artifacts. A timestamped directory is
        used when omitted.
    """

    loaded = load_config(config)
    declared_interface = InterfaceSpec.parse(interface)
    detected_runtime = declared_interface.detect_runtime(runtime)
    if detected_runtime == "matlab":
        raise NotImplementedError(
            "MATLAB entrypoints are recognized, but the 0.1.0a0 evaluator supports Python only."
        )
    if run_dir is None:
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        resolved_run_dir = Path.cwd() / "runs" / f"evolve_{stamp}"
    else:
        resolved_run_dir = Path(run_dir)
    engine = EvolutionEngine(
        initial=initial,
        interface=declared_interface,
        runtime=detected_runtime,
        editable=editable,
        config=loaded,
        run_dir=resolved_run_dir,
    )
    return engine.run()


__all__: list[str] = []
