"""Minimal unconstrained solver used by the quick-start experiment."""

from __future__ import annotations

import numpy as np


def solver(fun, x0):
    """Try coordinate steps until the evaluator stops the objective."""

    x = np.asarray(x0, dtype=float).copy()
    value = fun(x)
    step = np.maximum(1.0, np.abs(x))
    while True:
        improved = False
        for index in range(x.size):
            for sign in (-1.0, 1.0):
                trial = x.copy()
                trial[index] += sign * step[index]
                trial_value = fun(trial)
                if trial_value < value:
                    x, value = trial, trial_value
                    improved = True
        if not improved:
            step *= 0.5
        if np.max(step) < 1e-8:
            return x
