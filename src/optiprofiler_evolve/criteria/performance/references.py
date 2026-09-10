"""Trusted fixed-reference solvers used by paired OptiProfiler evaluations."""

from __future__ import annotations

from pathlib import Path

from optiprofiler_evolve.config import EvaluationConfig
from optiprofiler_evolve.criteria.performance.competitors import materialize_competitor
from optiprofiler_evolve.solver import InterfaceSpec, copy_initial_source


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
