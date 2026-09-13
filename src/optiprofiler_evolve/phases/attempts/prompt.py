"""Worker task cards for solver evolution."""

from __future__ import annotations

import json
from collections.abc import Sequence

from optiprofiler_evolve.agents.explanations import explanation_instructions
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.solver import InterfaceSpec

WORKER_PROMPT_VERSION = "solver-worker/6"


def build_worker_prompt(
    *,
    interface: InterfaceSpec,
    runtime: str,
    editable: Sequence[str],
    data: DataPlan,
    iteration: int,
    island: int,
    parent_score: float | None,
    controller_memory: str | None,
    token_budget: int | None,
    max_smoke_calls: int,
    max_public_calls: int,
    forbidden_candidate_imports: Sequence[str],
    direction: dict[str, object] | None = None,
    prompt_note: str | None = None,
) -> str:
    """Describe one bounded mutation job without exposing hidden data."""

    memory = controller_memory or "No cross-worker summary is available for this job."
    budget = str(token_budget) if token_budget is not None else "provider/default"
    parent_fitness = f"{parent_score:.6f}" if parent_score is not None else "unavailable"
    if forbidden_candidate_imports:
        dependency_policy = (
            "- This ablation forbids candidate imports from these existing solver APIs: "
            f"{json.dumps(list(forbidden_candidate_imports))}. The controller enforces this "
            "policy. You may use the Python standard library, NumPy, allowed numerical "
            "building blocks, and solver code you implement inside the editable repository. "
            "Do not evade the policy through dynamic imports or subprocesses."
        )
    else:
        dependency_policy = (
            "- Existing solver libraries installed in the worker environment may be used. "
            "Keep their use explicit in the candidate source so the trace is auditable."
        )
    if direction is None:
        direction_text = (
            "No scout direction is assigned. Explore an independent algorithmic direction "
            "based on the solver and public evidence."
        )
    else:
        direction_text = (
            "A read-only scout proposed the following hypothesis. Treat it as a testable "
            "starting point, not an instruction that must be preserved:\n"
            + json.dumps(direction, indent=2, sort_keys=True)
        )
    return f"""PROMPT_TEMPLATE: {WORKER_PROMPT_VERSION}

You are improving a derivative-free optimization solver.

Your job is to inspect and directly edit the solver repository in /workspace. Make a
coherent algorithmic improvement, not merely a blind hyperparameter sweep. You may add,
delete, or reorganize files inside the editable scope. Leave the best working version in
/workspace when you finish; do not return a patch as your only output.

When web research is enabled, use the harness WebSearch/WebFetch tools when the selected
model endpoint supports them. Do not assume that the shell has general network egress; if
native search is unavailable, continue from the local solver and evaluation evidence.

Contract
- Runtime: {runtime}
- Required OptiProfiler entrypoint: {interface.file}:{interface.function}
- Editable paths/globs: {json.dumps(list(editable))}
- Iteration: {iteration}; island: {island}
- Parent public fitness: {parent_fitness}; higher is better
- Advisory token budget: {budget}
{dependency_policy}

Evaluation tools
- Run `smoke_test` for rapid checks on a small public subset (at most
  {max_smoke_calls} calls in this job).
- Run `evaluate` for the canonical full public fitness (at most
  {max_public_calls} calls in this job).
- In Docker, if your shell cannot find those names, run
  `/opt/optiprofiler-evolve/tools/smoke_test` or
  `/opt/optiprofiler-evolve/tools/evaluate` directly. These are executables,
  not Python modules; a login shell may reset PATH.
- Each call permanently consumes one job-local quota slot, including a failed
  evaluation. If a tool reports `evaluation quota exhausted` or
  `retryable: false`, waiting cannot restore it: do not sleep or retry. Keep the
  best current solver and finish the job.
- Read the result.json, feedback.md, and benchmark_artifacts paths printed by those tools.
- Put rendered pages, downloads, scratch data, and other diagnostic outputs under `/tmp`,
  not `/workspace`. Before finishing, remove any diagnostic file accidentally created
  outside the declared editable paths; otherwise the strict scope audit rejects the
  candidate even when its solver code is valid.
- Prefer these bounded tools over calling the solver directly.
- MANDATORY: every local command that invokes the solver must cap objective evaluations
  and use a wall-clock guard such as `timeout 30s python test_solver.py`. Never launch an
  unbounded solver test or a bare Python command that may wait for solver convergence.
- Validation and hidden problems, scores, and mappings are controller-only. Do not try to
  discover them. Selection happens outside your workspace after public evaluation.

Required work order
1. Read the solver files needed to understand the current algorithm.
2. Run `smoke_test` once to establish a baseline.
3. Make a concrete solver edit before doing further exploratory experiments.
4. Run `smoke_test` again, then use `evaluate` when the candidate is ready.
5. Inspect the structured and visual benchmark artifacts, make any justified final edit,
   and finish with a bounded smoke check when quota remains.
6. {explanation_instructions()}
Do not spend the worker budget only studying or reimplementing the public problem. The
deliverable is an edited, valid solver in /workspace.
{_experiment_note(prompt_note)}
Public experiment manifest
{json.dumps(data.public_manifest(), indent=2, sort_keys=True)}

Controller memory
{memory}

Island direction
{direction_text}

Keep the declared solver signature valid. Use relative imports for solver-internal modules
so candidate and initial implementations can be loaded independently during evaluation. Keep
the solver deterministic unless the experiment explicitly needs stochastic behavior; use a
local generator with a stable seed rather than mutating global random state. You may write
your own local tests and use the enabled research tools. Do not attempt to modify
OptiProfiler, the problem library, evaluation tools, or files outside /workspace.
"""


def _experiment_note(prompt_note: str | None) -> str:
    """Render the run-owner work constraint; never a security or data boundary."""

    if prompt_note is None:
        return ""
    return (
        "\nExperiment note (must follow)\n"
        f"{prompt_note.strip()}\n"
        "This note constrains how you spend the work budget. It does not change "
        "the contract above, the evaluation tools, or any isolation rule.\n"
    )
