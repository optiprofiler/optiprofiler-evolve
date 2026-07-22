# Configuration

The config is strict: misspelled or unknown keys fail before a run starts.
`${ENV_NAME}` references are resolved from the environment, either as a complete
value or inside a non-secret CLI argument. Credential values belong under a
secret-named `workers.pool[].env` key so they are redacted in
`resolved_config.json`; never interpolate credentials into `args`.

This page explains how the major choices fit together. Use the
[configuration reference](config-reference.md) for every field and the checked-in
[JSON Schema](../config.schema.json) for editor completion and structural
validation. Runnable Claude, Codex, and multi-file inputs are indexed under
[Examples](../examples/README.md).

```yaml
data:
  library: s2mpj
  selection:
    ptype: u
    mindim: 1
    maxdim: 10
  split:
    validation_fraction: 0.2
    hidden_fraction: 0.2
    smoke_count: 5
    seed: 20260713

evaluation:
  backend: docker
  docker_image: optiprofiler-evolve-evaluator:latest
  timeout_seconds: 3600
  cpus: 4
  memory: 8g
  pids_limit: 512
  feedback_mode: agent
  reference: scipy_powell
  max_smoke_calls_per_worker: 20
  max_public_calls_per_worker: 5
  benchmark:
    max_eval_factor: 200
    n_jobs: 8
    score_only: false
    draw_hist_plots: parallel
  smoke_overrides:
    max_eval_factor: 20
    score_only: true

evolution:
  iterations: 10
  islands: 4
  attempts_per_island: 1
  population_per_island: 4
  migration_interval: 2
  random_seed: 0
  finalists_per_island: 1

workers:
  max_parallel: 4
  timeout_seconds: 1800
  token_budget: 50000
  max_budget_usd: 10
  pool:
    - harness: claude
      model: ${OPTIPROFILER_EVOLVE_MODEL}
      weight: 1
      pass_env: [ANTHROPIC_API_KEY]
    - harness: codex
      model: ${OPTIPROFILER_EVOLVE_CODEX_MODEL}
      weight: 1
      pass_env: [OPENAI_API_KEY]
      args: []
  tools:
    preset: research
    web_search: true
    network: true
    shell: true
    python: true
    git: true
    compilers: true
    package_install: false
    communication: controller_summary

integrity_review:
  component:
    name: agent_integrity
  worker:
    harness: claude
    model: ${OPTIPROFILER_EVOLVE_REVIEW_MODEL}
    pass_env: [ANTHROPIC_API_KEY]
  retries: 1
  strict: false
  timeout_seconds: 600
  token_budget: 12000

sandbox:
  backend: docker
  worker_image: optiprofiler-evolve-worker:latest
  cpus: 2
  memory: 4g
  pids_limit: 512
  max_candidate_files: 2000
  max_candidate_bytes: 200000000

workflow:
  phases:
    - name: prepare
    - name: explore
    - name: validate
    - name: hidden
    - name: report
  attempt_steps:
    - name: mutate
    - name: static_audit
    - name: smoke
    - name: public_evaluate
    - name: feedback
  after_iteration:
    - name: migration
```

The `workflow` block above is the built-in default and normally may be omitted.
It exists to make ablations explicit: phases define the run-level sequence,
attempt steps define one candidate's path, and after-iteration policies propose
population changes after all attempts reach the barrier. This is intentionally
an ordered protocol rather than an arbitrary DAG.

The optional full harness is shown in
[`examples/experiment-research.yaml`](../examples/experiment-research.yaml). It
inserts `direction_scout` before exploration, `strategy_analysis` and
`recombine` before final validation, and `challenger` after the one hidden
evaluation. See [Research workflow](research-workflow.md) for phase contracts,
failure behavior, and every built-in option.

`data.problem_names` can replace dynamic library selection. Explicit
`data.split.public`, `data.split.validation`, and `data.split.hidden` can
replace the random split. They must be disjoint and together contain the exact
selected universe. An explicit `data.split.smoke` may freeze a public subset.

Public evaluation is the worker-facing optimization signal. The controller also
evaluates every surviving candidate on validation, uses validation as the
primary population-retention and parent-selection score, and uses public fitness
as a tie-breaker. Hidden is evaluated only once for the fixed
validation-selected champion. Plan compute budgets accordingly: validation adds
one canonical benchmark call per surviving attempt.

Every surviving public candidate first passes a mandatory semantic integrity
review. The reviewer compares the candidate with its parent and inspects a
credential-redacted mutation transcript. It cannot call the evaluator. A
quarantined candidate consumes no validation query and cannot enter an island
archive. Use a distinct reviewer model for research runs; same-model reuse must
be explicit.

`runtime="auto"` in `evolve(...)` uses the interface suffix: `.py` selects the
Python adapter and `.m` selects the future MATLAB adapter. It describes the
entrypoint runtime, not every implementation language in the repository; a
Python wrapper may call compiled Fortran or C code.

`workers.pool` is weighted and may mix harnesses, providers, and models.
Provider-specific base URLs can be supplied through `env`, `pass_env`, or CLI
`args`. The package does not enumerate model vendors. Claude-compatible APIs
and Codex custom providers are not interchangeable: Codex custom providers must
implement the Responses protocol and function-tool loop. See
[Model providers and agent workers](providers.md) for complete templates and a
live agent-mode probe.

The built-in research image contains Python, Git, rg, `ddgr`, C/C++ build tools,
Codex, and Claude Code. `ddgr` is the fallback when a compatible model endpoint
does not expose a harness-native web-search tool. For strict tool-ablation
experiments, build separate `worker_image` variants. Only `web_search` and the
Claude-specific shell setting alter harness-visible tools; the other capability
fields cannot remove binaries already present in an image.

With `tools.network: false`, an internal Docker network blocks external access.
That mode requires a local/in-image model endpoint; a remote model API naturally
requires network access. `web_search: false` removes Claude's built-in web tools,
but strict network ablation also requires `network: false` or a controlled egress
image.

`tools.shell: false` removes `Bash` from Claude Code. Codex CLI does not expose an
equivalent no-shell agent mode, so the built-in adapter rejects that combination
instead of recording an ineffective ablation.

`sandbox.backend: unsafe_local` and `evaluation.backend: unsafe_local` exist for tests
and trusted development only. They execute worker or candidate code on the host
and are not substitutes for the default Docker boundaries. `token_budget` is an
advisory prompt budget; `max_budget_usd` is enforced by Claude Code per worker,
while Codex/provider-side limits must be configured at that provider.
