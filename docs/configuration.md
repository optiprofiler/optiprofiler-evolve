# Configuration

The config is strict: misspelled or unknown keys fail before a run starts.
Strings of the form `${ENV_NAME}` are resolved from the environment. Credential
values are redacted in `resolved_config.json`.

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
  rounds: 10
  islands: 4
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

sandbox:
  backend: docker
  worker_image: optiprofiler-evolve-worker:latest
  cpus: 2
  memory: 4g
  pids_limit: 512
  max_candidate_files: 2000
  max_candidate_bytes: 200000000
```

`data.problem_names` can replace dynamic library selection. Explicit
`data.split.public`, `data.split.validation`, and `data.split.hidden` can
replace the random split. They must be disjoint and together contain the exact
selected universe. An explicit `data.split.smoke` may freeze a public subset.

`runtime="auto"` in `evolve(...)` uses the interface suffix: `.py` selects the
Python adapter and `.m` selects the future MATLAB adapter. It describes the
entrypoint runtime, not every implementation language in the repository; a
Python wrapper may call compiled Fortran or C code.

`workers.pool` is weighted and may mix harnesses, providers, and models.
Provider-specific base URLs can be supplied through `env`, `pass_env`, or CLI
`args`. The package does not enumerate model vendors.

The built-in research image contains Python, Git, rg, `ddgr`, C/C++ build tools,
Codex, and Claude Code. `ddgr` is the fallback when a compatible model endpoint
does not expose a harness-native web-search tool. For strict tool-ablation
experiments, build separate `worker_image` variants; boolean tool fields
configure harness-visible tools but cannot remove binaries already present in
an image.

With `tools.network: false`, an internal Docker network blocks external access.
That mode requires a local/in-image model endpoint; a remote model API naturally
requires network access. `web_search: false` removes Claude's built-in web tools,
but strict network ablation also requires `network: false` or a controlled egress
image.

`sandbox.backend: unsafe_local` and `evaluation.backend: local` exist for tests
and trusted development only. They execute worker or candidate code on the host
and are not substitutes for the default Docker boundaries. `token_budget` is an
advisory prompt budget; `max_budget_usd` is enforced by Claude Code per worker,
while Codex/provider-side limits must be configured at that provider.
