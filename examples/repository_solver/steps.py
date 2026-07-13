"""Search steps used by the multi-file solver example."""

from __future__ import annotations

import numpy as np


def coordinate_trials(fun, x, value, step):
    """Try positive and negative coordinate moves once."""

    improved = False
    for index in range(x.size):
        for sign in (-1.0, 1.0):
            trial = x.copy()
            trial[index] += sign * step[index]
            trial_value = fun(trial)
            if trial_value < value:
                x, value = np.asarray(trial), trial_value
                improved = True
    return x, value, improved
