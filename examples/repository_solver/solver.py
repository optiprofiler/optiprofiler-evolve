"""Entrypoint for the multi-file solver example."""

from __future__ import annotations

import numpy as np

from .steps import coordinate_trials


def solver(fun, x0):
    """Run a minimal coordinate search implemented across two files."""

    x = np.asarray(x0, dtype=float).copy()
    value = fun(x)
    step = np.maximum(1.0, np.abs(x))
    while True:
        x, value, improved = coordinate_trials(fun, x, value, step)
        if not improved:
            step *= 0.5
        if np.max(step) < 1e-8:
            return x
