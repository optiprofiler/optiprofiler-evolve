# Research Workflow

The default workflow stops after population search, validation selection, one
hidden evaluation, and reporting. The optional research workflow keeps that
kernel and adds four bounded phases:

```text
prepare
  -> direction_scout
  -> explore
  -> strategy_analysis
  -> recombine
  -> validate
  -> hidden
  -> challenger
  -> report
```

It is enabled by the ordered `workflow.phases` list in
[`examples/experiment-research.yaml`](../examples/experiment-research.yaml).
There is no second public API and no generic DAG compiler.

The research workflow requires the `git` executable for fail-closed unified-patch
checks and application. The default lightweight workflow does not use this patch
path.

## 1. Direction scout

The scout receives a copy of the seed solver, its sanitized public benchmark
bundle, and the solver contract. It cannot edit the real seed and receives no
evaluation broker. It writes bounded direction cards which may guide selected
islands. Unguided islands remain independent controls.

| Option | Default | Meaning |
|---|---:|---|
| `mode` | `shared` | `off`, one `shared` scout, or `per_island` scouts. |
| `guided_islands` | first half | Island indexes receiving cards. |
| `max_directions` | `4` | Maximum normalized cards. |
| `worker_index` | `0` | Entry in `workers.pool`. |
| `timeout_seconds` | worker default | Role-specific wall time. |
| `token_budget` | worker default | Role-specific advisory token budget. |
| `max_budget_usd` | worker default | Role-specific Claude budget. |
| `tools` | network/web on | Role-specific `ToolConfig` overrides. |
| `prompt_version` | `direction-scout/1` | Recorded prompt contract. |

If a scout fails or returns invalid JSON, guided islands fall back to unguided
search and `directions.json` records `off-fallback`. No search iteration is lost.

## 2. Per-island strategy analysis

The controller validation-selects one finalist per island. An isolated analyst
receives copies of that finalist, seed, optional parent, source diff, exploration
trace, and the actual sanitized public plots/logs plus a compact evidence index.
It proposes strategy cards and executable leave-one-out variants.

Every toggle is a full variant tree or unified removal patch. The controller
materializes it in a new directory, applies the mandatory tree/interface/import/
edit-scope gates, and then evaluates it. Agent prose cannot earn evidence status:

- `Observed`: the concrete toggle ran successfully;
- `Inferred`: the agent supplied a code-grounded hypothesis not yet run;
- `Unverified`: materialization, gate, or evaluation failed.

An observed strategy is `supported` when removing it lowers public fitness by at
least `min_effect`, `contradicted` when removal improves fitness by that amount,
and otherwise `placebo`. `n_repeats: 1` is explicitly marked as one measurement,
not statistical certainty. Both the finalist and the ablated variant are freshly
evaluated `n_repeats` times; the exploration-time finalist score is retained only
as provenance. Paired per-problem inference still awaits OptiProfiler's versioned
`agent_report`.

Only `placebo` or `contradicted` strategies with a valid removal patch are
eligible for pruning. Failed or missing toggles remain `unverified`, and rejected
strategies that cannot be materialized are listed under
`unprunable_strategy_ids` rather than reported as removed.

| Option | Default | Meaning |
|---|---:|---|
| `max_strategies` | `6` | Maximum normalized cards per island. |
| `max_ablations` | `6` | Maximum executable toggles per island. |
| `min_effect` | `0.01` | Public-fitness effect threshold. |
| `n_repeats` | `1` | Fresh repeated evaluations of both finalist and ablated variant. |
| `worker_index` | `0` | Analyst worker entry. |
| `timeout_seconds` | worker default | Analyst wall time. |
| `token_budget` | worker default | Analyst advisory token budget. |
| `max_budget_usd` | worker default | Analyst Claude budget. |
| `tools` | network/web off | Analyst `ToolConfig` overrides. |
| `prompt_version` | `strategy-analysis/1` | Recorded prompt contract. |

Invalid or failed analysts produce zero verified strategies for that island and
the original validation-selected finalist continues.

## 3. Cross-island recombination

Only supported strategies with an explicit portable patch are eligible. The
controller first chooses one island bundle as the base. It then enumerates a
bounded set of patch combinations, never all `2^k` subsets. Every combination
passes the same mandatory solver gates and a real public evaluation. Patch
conflicts are recorded and skipped; they are never force-merged by another
agent. Controller validation returns selected IDs only, and the engine registers
those finalists for the normal `validate` phase.

| Option | Default | Meaning |
|---|---:|---|
| `max_strategies` | `8` | Maximum portable strategies considered. |
| `max_combination_size` | `2` | Maximum patches in one combination. |
| `max_combinations` | `12` | Hard evaluation cap for combinations. |
| `beam_width` | `3` | Validation-selected combinations registered. |

When no verified portable patch exists, the phase writes a clean skipped
artifact. Whole-tree variants remain useful for attribution but are not silently
treated as portable across different base trees.

## 4. Strong challenger

After the champion has been fixed and hidden has been evaluated once, the
challenger phase runs a separate public comparison against `scipy_powell`,
independent PRIMA `prima_newuoa`, or the initial solver. It cannot affect selection. The report preserves both solver
scores and warns that pairwise OptiProfiler scores depend on the competitor set,
so candidate-vs-seed and candidate-vs-challenger numbers are not interchangeable.

| Option | Default | Meaning |
|---|---:|---|
| `reference` | `scipy_powell` | `scipy_powell`, `prima_newuoa`, or `initial`. The evaluator image must contain PRIMA when `prima_newuoa` is selected. |

## Artifacts and failure semantics

Research artifacts live under `run_dir/research/` and include:

```text
directions.json
analysis/island-*/source.diff
analysis/island-*/evidence_manifest.json
analysis/island-*/strategy_cards.json
analysis/island-*/ablation/matrix.json
analysis/island_bundles.json
recombination.json
challenger_report.json
roles/<role>/<job>/
transcripts/<role>/
variants/
```

All JSON contracts carry a schema string. Agent workspaces contain only copied,
sanitized inputs. Scout and analyst roles receive no `smoke_test`, `evaluate`,
problem-name manifest, immutable reference, validation/hidden bundle, sibling
repository, host home, or Docker socket. The engine remains the sole writer of
candidate registration, validation selection, and authoritative events.

`research/validation_usage.json` separates controller selection calls from new
validation evaluations. Reusing a cached validation result increments the former
but not the latter. Phase-local artifacts report `validation_selection_calls`;
the controller ledger is authoritative for actual evaluator usage.

Phase-level resume is not implemented yet. A long research run can resume only
after the package gains an explicit phase checkpoint protocol; current iteration
checkpoints do not claim otherwise.
