# Public API Reference

The package has one public operation:

```python
from optiprofiler_evolve import evolve
```

## `evolve(...)`

```python
evolve(
    initial,
    *,
    interface="solver.py:solver",
    runtime="auto",
    editable=(".",),
    config,
    run_dir=None,
)
```

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `initial` | path-like | required | One solver file or a complete solver repository. It is copied before use and never edited in place. |
| `interface` | `str` | `"solver.py:solver"` | Entrypoint relative to the solver root, in `relative/file.py:function` form. |
| `runtime` | `"auto"`, `"python"`, or `"matlab"` | `"auto"` | Runtime of the entrypoint. Auto detection uses `.py` or `.m`. Python is implemented in the alpha; MATLAB currently fails before a run starts. |
| `editable` | sequence of paths/globs | `(".",)` | Files workers may add, edit, or delete. Every changed path must match at least one entry. |
| `config` | YAML path, mapping, or internal `EvolveConfig` | required | Complete data, evaluator, evolution, worker, tool, and sandbox experiment configuration. |
| `run_dir` | path-like or `None` | `None` | Empty or absent output directory. When omitted, a timestamped directory is created under `./runs/`. |

If `initial` is a single file, the copied solver root contains that file. The
`interface` filename must therefore match its basename, for example
`interface="bds.py:solver"`.

`editable` is enforced after each worker exits. Examples include:

```python
editable=["."]                    # the complete solver repository
editable=["*.py"]                 # Python source files at any depth
editable=["src", "tests"]         # two declared subtrees
```

Workers edit private copies. The controller rejects links, special files,
oversized trees, missing entrypoints, and changes outside this scope.

## Return value

After successful workflow completion, `evolve(...)` returns `EvolveResult`,
independently of whether the workflow includes report:

| Field | Meaning |
|---|---|
| `run_dir` | Complete experiment state and artifacts. |
| `best_solver` | Materialized `final_solver/`, otherwise the selected champion's candidate directory; `None` without a champion. |
| `best_candidate_id` | Candidate lineage identifier selected by validation, or `None`. |
| `public_score` | Candidate fitness on the public evolution set. |
| `validation_score` | Controller-only fitness used to select the champion. |
| `final_score` | Fitness on the hidden holdout for the validation-selected champion. |
| `finalists` | One hidden-assessed champion, or an empty tuple when hidden has not run. |
| `assessment_score` | Weighted formal criterion score for the selected champion, or `None`. |
| `final_assessment_score` | Weighted hidden criterion score, or `None` if unavailable. |
| `artifacts` | Detached top-level read-only mapping of committed phase outputs. Values may contain paths and typed records; this is not a deep snapshot or a public/sanitized export. |

Without a selected champion, all winner and score fields are `None`; a prepared
seed or explored population is not an implicit winner. Omitting hidden leaves
`final_score` and `final_assessment_score` as `None`. Disabled assessments do not
fabricate scores. Empty workflows return empty artifacts and no winner. Failures
and cancellation raise their original exceptions; no partial-success result is
returned. See [custom workflows](custom-workflows.md).

All fitness scores use `(candidate_score - reference_score + 1) / 2`. A score
of `0.5` is a tie with the configured fixed reference solver.

## Stable and internal surfaces

Only `evolve` and `__version__` are exported from the top-level package. Internal
dataclasses and modules may change during the alpha. Experiment reproducibility
should rely on the resolved config, frozen data manifest, solver contract, and
run artifacts rather than importing controller internals.
