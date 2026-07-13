# Configuration Reference

The YAML config controls one fixed experiment. It is strict: unknown keys and
invalid combinations fail before workers start. The checked-in
[JSON Schema](../config.schema.json) provides editor completion and structural
validation; Python validation in `config.py` remains authoritative for
cross-field rules.

Defaults shown below are package defaults. Fields marked **required** have no
usable default for a real run.

## Data

| Field | Type | Default | Meaning |
|---|---|---|---|
| `data.library` | string identifier | `s2mpj` | OptiProfiler problem-library adapter name. |
| `data.selection` | mapping | `{}` | Arguments passed to `<library>_select(...)` when exact names are not supplied. |
| `data.problem_names` | list of strings | `[]` | Exact selected universe; replaces dynamic selection when nonempty. |
| `data.custom_problem_libraries_path` | path or `null` | `null` | Directory containing an additional OptiProfiler problem-library implementation. |
| `data.split.hidden_fraction` | float in `[0, 1)` | `0.2` | Fraction withheld from workers when the split is generated. |
| `data.split.smoke_count` | positive integer | `3` | Number of public problems in the fast `smoke_test` subset. |
| `data.split.seed` | integer | `0` | Deterministic split and smoke-sampling seed. |
| `data.split.public` | list of strings | `[]` | Explicit public names. Must be supplied together with `hidden`. |
| `data.split.hidden` | list of strings | `[]` | Explicit hidden names. Must be supplied together with `public`. |

Explicit public and hidden lists must be disjoint and together contain the
entire selected universe. Smoke is always sampled from public. If the selected
universe contains one problem, hidden is empty even when `hidden_fraction` is
positive.

## Evaluation

| Field | Type | Default | Meaning |
|---|---|---|---|
| `evaluation.benchmark` | mapping | `{}` | Options forwarded to `optiprofiler.benchmark`; data and solver identity fields are controller-owned. |
| `evaluation.smoke_overrides` | mapping | `{}` | Benchmark options overlaid only for `smoke_test`. |
| `evaluation.backend` | `docker` or `local` | `docker` | Execution boundary for Python candidate code. `local` is trusted-development only. |
| `evaluation.docker_image` | string or `null` | `null` | Evaluator image; **required** when backend is `docker`. |
| `evaluation.timeout_seconds` | positive integer | `3600` | Wall-time limit for one Docker evaluator process; the trusted local backend has no process timeout. |
| `evaluation.cpus` | positive number | `4.0` | CPU limit passed to the evaluator container; ignored by the local backend. |
| `evaluation.memory` | Docker memory string | `8g` | Evaluator-container memory limit; ignored by the local backend. |
| `evaluation.pids_limit` | integer at least `16` | `512` | Evaluator-container process limit; ignored by the local backend. |
| `evaluation.feedback_mode` | `summary` or `agent` | `summary` | Amount of structured benchmark feedback exposed after public evaluations. |
| `evaluation.max_smoke_calls_per_worker` | nonnegative integer | `20` | Broker quota for worker `smoke_test` calls. |
| `evaluation.max_public_calls_per_worker` | nonnegative integer | `5` | Broker quota for worker `evaluate` calls. |

`load` and `solvers_to_load` are rejected in both benchmark mappings. Candidate
and immutable initial solver must be executed together in each canonical
benchmark call. The controller overrides `plibs`, `problem_names`, solver names,
normalization, and other identity-sensitive fields.

## Evolution

| Field | Type | Default | Meaning |
|---|---|---|---|
| `evolution.rounds` | positive integer | `3` | Number of generations; one worker is dispatched per island per round. |
| `evolution.islands` | positive integer | `4` | Number of independently maintained populations. |
| `evolution.population_per_island` | positive integer | `4` | Maximum retained candidates in each island. |
| `evolution.migration_interval` | nonnegative integer | `2` | Champion ring-migration interval; `0` disables migration. |
| `evolution.random_seed` | integer | `0` | Reproducible controller sampling seed. |
| `evolution.finalists_per_island` | positive integer | `1` | Fixed candidates per island sent to controller-only final evaluation. |

`finalists_per_island` cannot exceed `population_per_island`.

## Worker pool

| Field | Type | Default | Meaning |
|---|---|---|---|
| `workers.pool` | list of worker mappings | **required** | Weighted Codex/Claude worker choices. At least one is required. |
| `workers.max_parallel` | positive integer | `4` | Maximum concurrent coding workers and final evaluator calls. |
| `workers.timeout_seconds` | positive integer | `1800` | Wall-time limit for one coding-agent process. |
| `workers.token_budget` | positive integer or `null` | `null` | Advisory token budget inserted into the worker task. |
| `workers.max_budget_usd` | positive number or `null` | `null` | Claude Code per-worker cost limit. Other providers enforce their own limits. |
| `workers.pool[].harness` | `claude` or `codex` | **required** | Coding-agent CLI adapter. |
| `workers.pool[].model` | string | **required** | Model identifier passed unchanged to the selected CLI. |
| `workers.pool[].weight` | positive integer | `1` | Relative probability of selecting this worker entry. |
| `workers.pool[].profile` | string or `null` | `null` | Optional Codex profile name. |
| `workers.pool[].args` | list of strings | `[]` | Extra CLI arguments appended to the harness command. |
| `workers.pool[].env` | string mapping | `{}` | Explicit environment values or `${ENV_NAME}` references passed to the worker. |
| `workers.pool[].pass_env` | list of names | `[]` | Host environment variables copied into the worker; missing names fail early. |

Do not store credentials directly in YAML. Prefer `pass_env` or an
`${ENV_NAME}` reference. Keys containing common credential markers are redacted
from `resolved_config.json`.

## Worker tools

| Field | Type | Default | Meaning |
|---|---|---|---|
| `workers.tools.preset` | `minimal`, `research`, or `custom` | `research` | Named tool policy recorded for the experiment. |
| `workers.tools.web_search` | boolean | `true` | Expose harness web-search tools when supported. Requires network. |
| `workers.tools.network` | boolean | `true` | Permit worker-container outbound networking. |
| `workers.tools.shell` | boolean | `true` | Expose shell execution through the harness. |
| `workers.tools.python` | boolean | `true` | Declare Python available to workers. Strict removal requires a different image. |
| `workers.tools.git` | boolean | `true` | Declare Git available to workers. Strict removal requires a different image. |
| `workers.tools.compilers` | boolean | `true` | Declare compiler tools available. Strict removal requires a different image. |
| `workers.tools.package_install` | boolean | `false` | Declare package installation permitted; image and network policy still apply. |
| `workers.tools.communication` | `none`, `controller_summary`, `island`, or `global` | `none` | Previous-attempt information injected into later worker prompts. |

Boolean availability fields describe the harness policy. They cannot remove a
binary already present in an image. Use separate worker images for strict tool
ablation studies.

## Sandbox

| Field | Type | Default | Meaning |
|---|---|---|---|
| `sandbox.backend` | `docker` or `unsafe_local` | `docker` | Coding-worker execution boundary. `unsafe_local` is only for trusted tests. |
| `sandbox.worker_image` | string | `optiprofiler-evolve-worker:latest` | Docker image containing supported coding CLIs and declared tools. |
| `sandbox.cpus` | positive number | `2.0` | CPU limit for one worker container. |
| `sandbox.memory` | Docker memory string | `4g` | Worker-container memory limit. |
| `sandbox.pids_limit` | integer at least `16` | `512` | Worker-container process limit. |
| `sandbox.max_candidate_files` | positive integer | `2000` | Maximum files accepted from one worker workspace. |
| `sandbox.max_candidate_bytes` | positive integer | `200000000` | Maximum total bytes accepted from one worker workspace. |

The worker receives only a private solver copy, public manifests, and bounded
evaluation tools. It does not receive the immutable reference, hidden manifest,
OptiProfiler source, Docker socket, host home, or sibling repositories.

## Provider configuration

The package passes model names, environment variables, profiles, and extra
arguments to Codex or Claude Code. It does not maintain a vendor registry. A
compatible provider can be configured in one worker entry, for example:

```yaml
workers:
  pool:
    - harness: codex
      model: ${OPTIPROFILER_EVOLVE_CODEX_MODEL}
      env:
        OPENAI_BASE_URL: ${OPTIPROFILER_EVOLVE_OPENAI_BASE_URL}
      pass_env: [OPENAI_API_KEY]
```

Use the environment-variable names and model identifier expected by the
installed CLI/provider. Provider support should be verified with that CLI
before starting a multi-round experiment.
