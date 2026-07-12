"""Worker task cards for solver evolution."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .data import DataPlan
from .solver import InterfaceSpec


def build_worker_prompt(
    *,
    interface: InterfaceSpec,
    runtime: str,
    editable: Sequence[str],
    data: DataPlan,
    generation: int,
    island: int,
    parent_score: float,
    controller_memory: str | None,
    token_budget: int | None,
) -> str:
    """Describe one bounded mutation job without exposing hidden data."""

    memory = controller_memory or "No cross-worker summary is available for this job."
    budget = str(token_budget) if token_budget is not None else "provider/default"
    return f"""You are improving a derivative-free optimization solver.

Your job is to inspect and directly edit the solver repository in /workspace. Make a
coherent algorithmic improvement, not merely a blind hyperparameter sweep. You may add,
delete, or reorganize files inside the editable scope. Leave the best working version in
/workspace when you finish; do not return a patch as your only output.

Contract
- Runtime: {runtime}
- Required OptiProfiler entrypoint: {interface.file}:{interface.function}
- Editable paths/globs: {json.dumps(list(editable))}
- Generation: {generation}; island: {island}
- Parent public fitness: {parent_score:.6f}; 0.5 means tied with the immutable initial solver
- Advisory token budget: {budget}

Evaluation tools
- Run `smoke_test` for rapid checks on a small public subset.
- Run `evaluate` for the canonical full public fitness.
- Read the result.json and feedback.md paths printed by those tools.
- Hidden problems and final evaluation are controller-only. Do not try to discover them.

Public experiment manifest
{json.dumps(data.public_manifest(), indent=2, sort_keys=True)}

Controller memory
{memory}

Keep the declared solver signature valid. Use relative imports for solver-internal modules
so candidate and initial implementations can be loaded independently during evaluation.
You may write your own local tests and use the enabled research tools. Do not attempt to
modify OptiProfiler, the problem library, evaluation tools, or files outside /workspace.
"""


__all__: list[str] = []
