# Configuration

Configure standards and their independent weights under `assessment.criteria`.
For source simplicity, enable `complexity` with `options: {max_ratio: 1.2}`.
Use `mode: enforce` for a hard constraint or `warn` for a soft score and advice.
See [Algorithm assessment](assessment.md) for profiles, scoring and migration.

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
evolution:
  iterations: 10
  islands: 4
  attempts_per_island: 1
  population_per_island: 4
  migration_interval: 0
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
    pass_env:
    - ANTHROPIC_API_KEY
    provider_gateway:
      upstream_base_url: https://api.anthropic.com
      credential_env: ANTHROPIC_API_KEY
      auth_mode: x-api-key
  - harness: codex
    model: ${OPTIPROFILER_EVOLVE_CODEX_MODEL}
    weight: 1
    pass_env:
    - OPENAI_API_KEY
    args: []
    provider_gateway:
      upstream_base_url: https://api.openai.com/v1
      credential_env: OPENAI_API_KEY
      auth_mode: bearer
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
workflow:
  phases:
  - name: prepare
  - name: explore
  - name: validate
  - name: hidden
  - name: report
  attempt_steps:
  - name: mutate
  - name: smoke
  - name: public_evaluate
  after_iteration: []
execution:
  backend: docker
  docker_image: optiprofiler-evolve-evaluator:latest
  timeout_seconds: 3600
  cpus: 4
  memory: 8g
  pids_limit: 512
  feedback_mode: full_artifacts
  max_smoke_calls_per_worker: 20
  max_public_calls_per_worker: 5
assessment:
  criteria:
    performance:
      options:
        reference: scipy_powell
        benchmark:
          max_eval_factor: 200
          n_jobs: 8
          score_only: false
          draw_hist_plots: parallel
        smoke_overrides:
          max_eval_factor: 20
          score_only: true
    integrity:
      options:
        component:
          name: agent_integrity
        worker:
          harness: claude
          model: ${OPTIPROFILER_EVOLVE_REVIEW_MODEL}
          pass_env:
          - ANTHROPIC_API_KEY
          provider_gateway:
            upstream_base_url: https://api.anthropic.com
            credential_env: ANTHROPIC_API_KEY
            auth_mode: x-api-key
        retries: 1
        timeout_seconds: 600
        token_budget: 12000
    complexity:
      enabled: false
      mode: warn
      options: {}
```

The `workflow` block above is the built-in default and normally may be omitted.
Calling `evolve(..., config=None)` further uses a one-iteration, one-island
quick start. Optional `static_audit` and `feedback` steps, whole-candidate
migration, web search, compilers, and the research phases must be requested
explicitly. The engine's final source/interface/import gate remains mandatory;
quality criteria have independent enable switches. The workflow is an ordered
protocol rather than an arbitrary DAG.

The optional full harness is shown in
[`examples/experiment-research.yaml`](../examples/experiment-research.yaml). It
inserts `direction_scout` before exploration, `strategy_analysis` and
`strategy_transfer` before final validation, and `challenger` after the one
hidden execution. See [Research workflow](research-workflow.md) for phase
contracts, failure behavior, explanation artifacts, and every built-in option.

`data.problem_names` can replace dynamic library selection. Explicit
`data.split.public`, `data.split.validation`, and `data.split.hidden` can
replace the random split. They must be disjoint and together contain the exact
selected universe. An explicit `data.split.smoke` may freeze a public subset.

Public-profile assessment guides workers. Formal assessment produces the
weighted score used for default retention, parent sampling and champion
selection. Enabled performance assessment adds a validation benchmark per
surviving candidate. Hidden evaluates only the fixed champion and never selects
a replacement. Raw benchmark scores remain separate from weighted totals.

The default formal profile includes semantic integrity review, which can be
disabled like any other standard. The reviewer compares parent and candidate and inspects a
credential-redacted mutation transcript. It cannot call the evaluator. A
quarantined candidate under `mode: enforce` consumes no validation benchmark and
cannot enter an island archive. Use a distinct reviewer model; same-model reuse must
be explicit.

`runtime="auto"` in `evolve(...)` uses the interface suffix: `.py` selects the
Python adapter and `.m` selects the future MATLAB adapter. It describes the
entrypoint runtime, not every implementation language in the repository; a
Python wrapper may call compiled Fortran or C code.

`workers.pool` is weighted and may mix harnesses, providers, and models.
Each worker pins its provider origin, credential name, and wire protocol under
`provider_gateway`. The controller keeps the real credential in a per-invocation
gateway and generates the CLI's internal route. Claude-compatible APIs and Codex
custom providers are not interchangeable: Codex custom providers must implement
the Responses protocol and function-tool loop. See
[Model providers and agent workers](providers.md) for complete templates and a
live agent-mode probe.

The built-in research image contains Python, Git, rg, `ddgr`, C/C++ build tools,
Codex, and Claude Code. In gateway mode, installed network clients such as
`ddgr` do not have shell egress; use harness-native search when the pinned
provider supports it. For strict tool-ablation experiments, build separate
`worker_image` variants. Only `web_search` and the Claude-specific shell setting
alter harness-visible tools; the other capability fields cannot remove binaries
already present in an image.

Provider transport is independent of worker egress. A gateway-routed worker is
always on an internal network, so shell and package-manager egress remain
blocked even when `tools.network: true` permits a native network-backed harness
tool. `web_search: false` removes harness-native search; model transport remains
available through the sidecar.

`tools.shell: false` removes `Bash` from Claude Code. Codex CLI does not expose an
equivalent no-shell agent mode, so the built-in adapter rejects that combination
instead of recording an ineffective ablation.

`sandbox.backend: unsafe_local` and `execution.backend: unsafe_local` exist for tests
and trusted development only. They execute worker or candidate code on the host
and are not substitutes for the default Docker boundaries. `token_budget` is an
advisory prompt budget; `max_budget_usd` is enforced by Claude Code per worker,
while Codex/provider-side limits must be configured at that provider.
