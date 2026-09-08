"""Trusted fixed-reference solvers used by paired OptiProfiler evaluations."""

from __future__ import annotations

from pathlib import Path

from .competitors import materialize_competitor
from .config import EvaluationConfig
from .solver import InterfaceSpec, copy_initial_source


def materialize_reference(
    *,
    initial: Path,
    destination: Path,
    interface: InterfaceSpec,
    config: EvaluationConfig,
    ptype: str | None = None,
) -> Path:
    """Copy the initial solver or create a trusted built-in reference tree."""

    if config.reference == "initial":
        return copy_initial_source(initial, destination)

    return materialize_competitor(
        config.reference,
        destination=destination,
        interface=interface,
        max_eval_factor=_reference_budget_factor(config),
        ptype=ptype,
    )


def _reference_budget_factor(config: EvaluationConfig) -> float:
    factor = float(config.benchmark.get("max_eval_factor", 500))
    if factor <= 0:
        raise ValueError("benchmark.max_eval_factor must be positive.")
    return factor


__all__: list[str] = []
