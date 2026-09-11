# Research Workflow

`evolve(...)` has one small default path:

```text
prepare -> explore -> validate -> hidden -> report
```

Each default exploration attempt runs only `mutate -> smoke ->
public_evaluate`. Whole-candidate migration, web search, compilers, additional
audit evidence, feedback injection, and research phases are explicit opt-ins.
Controller execution safety remains mandatory. Quality standards are configured
independently; semantic integrity is enabled for formal assessment by default.

For strategy-level research, use the ordered workflow in
[`examples/experiment-research.yaml`](../examples/experiment-research.yaml):

```text
prepare
  -> direction_scout
  -> explore
  -> strategy_analysis
  -> strategy_transfer
  -> validate
  -> hidden
  -> challenger
  -> report
```

This remains one controller-owned workflow, not a DAG compiler or a second
public API.

## 1. Direction scout

The optional scout receives a copy of the seed, the solver contract, and
sanitized public evidence. It proposes bounded direction cards before search.
Only configured islands receive cards; the remaining islands are independent
controls. Scout failure is recorded and search continues unguided.

The `mode` option is `off`, `shared`, or `per_island`. Other limits cover the
number of directions, guided island indexes, worker selection, tools, time,
tokens, and cost. The scout cannot edit the seed and receives no evaluation
broker, validation data, or hidden data.

## 2. Per-island strategy analysis

The controller validation-selects one finalist from each island. An isolated
analyst receives copies of the seed, finalist, optional parent, source diff,
exploration trace, and the finalist's sanitized public benchmark bundle. It
must describe code-bound mechanisms and produce an executable leave-one-out
variant for every mechanism that can safely be separated.

For each proposed strategy, the controller independently evaluates both the
finalist and its ablated variant. The default `evaluation_mode: smoke` keeps
this pass bounded; `public` requests canonical public execution. Removing a
strategy yields one of three controller-owned conclusions:

- `supported`: removal reduces fitness by at least `min_effect`;
- `contradicted`: removal improves fitness by at least `min_effect`;
- `placebo`: the absolute measured change is smaller than `min_effect`.

Materialization, gate, or evaluation failures remain `unverified`. An agent's
prose never assigns its own evidence status. With `n_repeats: 1`, the artifact
states that this is a single measurement, not statistical certainty.

The analyst can inspect existing profile and history plots through the public
evidence index. Reliable automatic selection of the most discriminative
problems still requires a dedicated public evidence projection. The private
EvalReport reader now exists in observability, but is not wired into research
agents. The evidence index consumes only the existing projected benchmark bundle,
not a speculative `agent_report.json` format.
Evolve does not infer problem-level effects from filenames or redefine Core's
report schema. See [EvalReport evidence](architecture.md#evalreport-evidence).
Each ablation entry keeps the base and removed-variant artifact directories, so
the owner can compare their profile and history plots instead of relying only
on the scalar effect.

## 3. Cross-island strategy transfer

The recommended cross-island phase transfers only semantic strategy cards
whose leave-one-out tests were `supported` on their source island. It never
copies a donor island's complete solver tree.

For each target island:

1. start from that island's own selected bundle;
2. offer a bounded set of supported strategies from other islands;
3. let one isolated integrator apply only compatible mechanisms;
4. run normal source/interface/import gates and public evaluation;
5. run the independent integrity reviewer;
6. validation-select between the unchanged island base and integrated variants;
7. retain exactly one winner for that island.

The transfer manifest records attempted, applied, and rejected strategy IDs.
The final explanation distinguishes source-island ablation evidence from the
combined target-island selection result; it does not claim that each imported
mechanism was independently attributed on the target.

Useful options are `max_strategies_per_island`, `attempts_per_island`,
`worker_index`, role-specific tools, time, tokens, cost, and `prompt_version`.

## 4. Why whole-code migration defaults to off

`evolution.migration_interval: 0` and an empty `workflow.after_iteration` keep
whole candidates inside their original island by default. This makes island
diversity, strategy attribution, and transfer ablations easier to interpret.

This is an OptiProfiler Evolve experimental default, not a claim that mature
island systems universally disable migration. [OpenEvolve exposes configurable
periodic migration](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/configs/default_config.yaml),
while [FunSearch periodically resets weaker islands from stronger
ones](https://www.nature.com/articles/s41586-023-06924-6). Both remain useful
ablation baselines. To reproduce a whole-code migration study, add the built-in
`migration` after-iteration policy explicitly.

The older `recombine` phase is also retained for compatibility with experiments
that produce portable patches against a common seed. It is not in the
recommended workflow because patch portability and mechanism portability are
not the same claim.

## 5. Strong challenger

After champion selection and the one-shot hidden evaluation, `challenger` runs
a separate public comparison against any registered compatible competitor.
This result cannot affect selection. Pairwise OptiProfiler scores depend on the
competitor set, so candidate-versus-seed and candidate-versus-challenger scores
are preserved as separate evidence.

## Solver explanations

Every worker that emits a solver is asked to write
`.optiprofiler_evolve/solver_explanation.json`. The controller validates and
moves this untrusted metadata outside the executable solver tree before audit
and execution. Missing or malformed explanations are visible but do not turn
into a numerical rejection.

The final run contains:

```text
final_solver/                    executable champion
final_solver.diff                exact seed-to-champion source diff
FINAL_SOLVER_EXPLANATION.md      summary, pseudocode, mechanisms, limits, evidence
explanations/*.json              normalized per-attempt explanations
```

## Research artifacts

```text
research/directions.json
research/analysis/island-*/source.diff
research/analysis/island-*/evidence_manifest.json
research/analysis/island-*/strategy_cards.json
research/analysis/island-*/ablation/matrix.json
research/analysis/island_bundles.json
research/strategy_transfer.json
research/transfer/island-*/winner_solver/
research/challenger_report.json
research/roles/<role>/<job>/
research/transcripts/<role>/
research/variants/
```

All machine-readable contracts carry schema strings. Agent workspaces contain
only copied, sanitized inputs. The engine remains the sole writer of candidate
registration, validation selection, authoritative events, and final promotion.

Phase-level resume is not implemented yet. Iteration checkpoints do not claim
that an interrupted strategy-analysis or transfer phase can resume midway.
