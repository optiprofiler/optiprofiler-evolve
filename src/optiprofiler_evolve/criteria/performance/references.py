"""Trusted fixed-reference solvers used by paired OptiProfiler evaluations."""

from __future__ import annotations

from pathlib import Path

from optiprofiler_evolve.criteria.performance.competitors import materialize_competitor
from optiprofiler_evolve.solver import InterfaceSpec, copy_initial_source


def materialize_reference(
    *,
    initial: Path,
    destination: Path,
    interface: InterfaceSpec,
    reference: str,
    max_eval_factor: float,
    ptype: str | None = None,
) -> Path:
    """Copy the initial solver or create a trusted built-in reference tree."""

    if reference == "initial":
        return copy_initial_source(initial, destination)

    return materialize_competitor(
        reference,
        destination=destination,
        interface=interface,
        max_eval_factor=max_eval_factor,
        ptype=ptype,
    )
