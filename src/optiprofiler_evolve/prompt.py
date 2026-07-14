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

When web research is enabled, prefer the harness WebSearch/WebFetch tools. Some compatible
model endpoints do not expose those built-in tools; in that case use
`ddgr --json --num 5 "your query"` from the shell instead of repeated ad hoc curl attempts.

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
- Prefer these bounded tools over calling the solver directly.
- MANDATORY: every local command that invokes the solver must cap objective evaluations
  and use a wall-clock guard such as `timeout 30s python test_solver.py`. Never launch an
  unbounded solver test or a bare Python command that may wait for solver convergence.
- Hidden problems and final evaluation are controller-only. Do not try to discover them.

Required work order
1. Read the solver files needed to understand the current algorithm.
2. Run `smoke_test` once to establish a baseline.
3. Make a concrete solver edit before doing further exploratory experiments.
4. Run `smoke_test` again, then use `evaluate` when the candidate is ready.
Do not spend the worker budget only studying or reimplementing the public problem. The
deliverable is an edited, valid solver in /workspace.

Public experiment manifest
{json.dumps(data.public_manifest(), indent=2, sort_keys=True)}

Controller memory
{memory}

Keep the declared solver signature valid. Use relative imports for solver-internal modules
so candidate and initial implementations can be loaded independently during evaluation. Keep
the solver deterministic unless the experiment explicitly needs stochastic behavior; use a
local generator with a stable seed rather than mutating global random state. You may write
your own local tests and use the enabled research tools. Do not attempt to modify
OptiProfiler, the problem library, evaluation tools, or files outside /workspace.
"""


__all__: list[str] = []
