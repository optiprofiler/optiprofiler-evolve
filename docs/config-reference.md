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
| `data.split.validation_fraction` | float in `[0, 1)` | `0.2` | Controller-only fraction used to select and retain candidates. |
| `data.split.hidden_fraction` | float in `[0, 1)` | `0.2` | Final holdout fraction evaluated once after champion selection. |
| `data.split.smoke_count` | positive integer | `3` | Number of public problems in the fast `smoke_test` subset. |
| `data.split.seed` | integer | `0` | Deterministic split and smoke-sampling seed. |
| `data.split.public` | list of strings | `[]` | Explicit worker-facing training set. A nonempty value enables explicit splitting. |
| `data.split.validation` | list of strings | `[]` | Explicit controller-only selection set. |
| `data.split.hidden` | list of strings | `[]` | Explicit controller-only final holdout set. |
| `data.split.smoke` | list of strings | `[]` | Optional explicit fast subset; it must be contained in `public`. |

Explicit public, validation, and hidden lists must be pairwise disjoint and
together contain the entire selected universe. Smoke is always a public subset.
Workers receive only the two evaluation capabilities and an anonymous count/hash
manifest; names, aliases, validation results, and hidden results remain in the
controller directory.

## Evaluation

| Field | Type | Default | Meaning |
|---|---|---|---|
| `evaluation.benchmark` | mapping | `{}` | Options forwarded to `optiprofiler.benchmark`; data and solver identity fields are controller-owned. |
| `evaluation.smoke_overrides` | mapping | `{}` | Benchmark options overlaid only for `smoke_test`. |
| `evaluation.backend` | `docker` or `unsafe_local` | `docker` | Execution boundary for Python candidate code. `unsafe_local` executes untrusted solver code in the controller process and is for trusted development only. |
| `evaluation.docker_image` | string or `null` | `null` | Evaluator image; **required** when backend is `docker`. |
| `evaluation.timeout_seconds` | positive integer | `3600` | Wall-time limit for one Docker evaluator process; `unsafe_local` has no process timeout. |
| `evaluation.cpus` | positive number | `2.0` | CPU limit passed to the evaluator container; ignored by `unsafe_local`. |
| `evaluation.memory` | Docker memory string | `4g` | Evaluator-container memory limit; ignored by `unsafe_local`. |
| `evaluation.pids_limit` | integer at least `16` | `512` | Evaluator-container process limit; ignored by `unsafe_local`. |
| `evaluation.feedback_mode` | `summary` or `agent` | `summary` | Amount of structured benchmark feedback exposed after public evaluations. |
| `evaluation.reference` | `initial`, `scipy_powell`, or `prima_newuoa` | `initial` | Fixed reference solver paired with every candidate benchmark. Use `initial` for seed-relative evolution; reserve a strong solver for post-selection comparison. |
| `evaluation.forbidden_candidate_imports` | list of dotted Python module names | `[]` | Reject candidate source that uses these imports. This is an auditable experiment ablation, not a security boundary. |
| `evaluation.max_smoke_calls_per_worker` | nonnegative integer | `20` | Broker quota for worker `smoke_test` calls. |
| `evaluation.max_public_calls_per_worker` | nonnegative integer | `5` | Broker quota for worker `evaluate` calls. |
| `evaluation.adapter` | registered name | `optiprofiler` | Trusted evaluator adapter. Change this only for an owner-supplied evaluator implementation. |

Evaluation quotas are local to one worker job and each invocation consumes one
slot, including a failed evaluation. An exhausted quota is non-retryable;
waiting does not restore it.

When a post-selection challenger such as `prima_newuoa` is installed in the
evaluator image, add its import root (`prima`) to
`evaluation.forbidden_candidate_imports` if candidates must remain independent
of that implementation. Apply the same rule to any other installed solver API
excluded by the experiment contract, such as `scipy.optimize`. This check makes
the policy auditable; it is not a sandbox security boundary.

`load` and `solvers_to_load` are rejected in both benchmark mappings. Candidate
and the fixed reference solver must be executed together in each canonical
benchmark call. The controller overrides `plibs`, `problem_names`, solver names,
normalization, and other identity-sensitive fields.

## Evolution

| Field | Type | Default | Meaning |
|---|---|---|---|
| `evolution.iterations` | positive integer | `3` | Number of synchronized population iterations. |
| `evolution.islands` | positive integer | `4` | Number of independently maintained populations. |
| `evolution.attempts_per_island` | positive integer | `1` | Candidate attempts launched for each island in one iteration. |
| `evolution.population_per_island` | positive integer | `4` | Maximum retained candidates in each island. |
| `evolution.migration_interval` | nonnegative integer | `2` | Champion ring-migration interval; `0` disables migration. |
| `evolution.random_seed` | integer | `0` | Reproducible controller sampling seed. |
| `evolution.finalists_per_island` | positive integer | `1` | Validation-ranked candidates per island considered when selecting one champion. |
| `evolution.retention.name` | registered retention name | `validation_lexicographic` | Bounded island-archive retention strategy. `metric_pareto` enables multi-objective research variants. |
| `evolution.retention.options` | object | `{}` | Constructor options for the retention strategy, such as Pareto objectives and epsilon. |
| `evolution.parent_sampler.name` | registered sampler name | `top_biased_validation_weighted` | Parent selection strategy applied to the retained island archive. |
| `evolution.parent_sampler.options` | object | `{"greedy_ratio": 0.7}` | Constructor options for the parent sampler. |

`finalists_per_island` cannot exceed `population_per_island`.

## Worker pool

| Field | Type | Default | Meaning |
|---|---|---|---|
| `workers.pool` | list of worker mappings | **required** | Weighted Codex/Claude worker choices. At least one is required. |
| `workers.max_parallel` | positive integer | `4` | Maximum concurrent coding workers and final evaluator calls. |
| `workers.timeout_seconds` | positive integer | `1800` | Wall-time limit for one coding-agent process. |
| `workers.token_budget` | positive integer or `null` | `null` | Advisory token budget inserted into the worker task. |
| `workers.max_budget_usd` | positive number or `null` | `null` | Claude Code per-worker cost limit. Other providers enforce their own limits. |
| `workers.adapter` | registered name | `cli` | Worker lifecycle adapter. The built-in adapter runs Codex or Claude Code. |
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

`profile` is useful only when the selected worker image contains the matching
Codex profile under its own `CODEX_HOME`. The default image uses an ephemeral
home and deliberately does not inherit host Codex profiles. Prefer explicit
provider `args` for portable experiment configs.

## Integrity review

The semantic integrity gate is mandatory and runs after public checks but before
any validation query or population admission. The default implementation uses a
separate coding-agent invocation with no evaluation broker, network, problem
manifest, population, validation data, or hidden data. It sees sanitized copies
of the parent, candidate, changed-file list, solver contract, and mutation
transcript.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `integrity_review.component.name` | registered reviewer name | `agent_integrity` | Reviewer implementation. `unsafe_approve` is restricted to explicit tests/ablations. |
| `integrity_review.component.options` | object | `{}` | Constructor options for an owner-supplied reviewer. |
| `integrity_review.worker` | worker mapping or `null` | `null` | Dedicated reviewer worker. Required unless same-model reuse is explicitly allowed. |
| `integrity_review.allow_same_model` | boolean | `false` | Permit reuse of the first mutation worker identity. This is an explicit ablation, not the recommended research setup. |
| `integrity_review.allow_unsafe_stub` | boolean | `false` | Required second opt-in when `component.name` is `unsafe_approve`. |
| `integrity_review.retries` | nonnegative integer | `1` | Additional attempts after malformed output or reviewer unavailability. |
| `integrity_review.strict` | boolean | `false` | Abort the run when all reviewer attempts are unavailable. Evidence-backed quarantine still rejects only that candidate. |
| `integrity_review.timeout_seconds` | positive integer | `600` | Wall-time limit for one reviewer invocation. |
| `integrity_review.token_budget` | positive integer or `null` | `4000` | Reviewer-specific advisory token budget. |
| `integrity_review.max_budget_usd` | positive number or `null` | `null` | Reviewer-specific Claude Code cost cap. |

The nested `integrity_review.worker` accepts the same fields as
`workers.pool[]`: `harness`, `model`, `weight`, `profile`, `args`, `env`, and
`pass_env`. When `allow_same_model` is false, its harness/model identity must not
appear in the mutation pool. Every review attempt retains a complete private
trace under `research/traces/integrity-reviewer/`.

## Worker tools

| Field | Type | Default | Meaning |
|---|---|---|---|
| `workers.tools.preset` | `minimal`, `research`, or `custom` | `research` | Named tool policy recorded for the experiment. |
| `workers.tools.web_search` | boolean | `true` | Expose harness web-search tools when supported. Requires network. |
| `workers.tools.network` | boolean | `true` | Permit worker-container outbound networking. |
| `workers.tools.shell` | boolean | `true` | Include `Bash` in Claude Code's tool list. The built-in Codex adapter cannot enforce `false` and rejects that combination. |
| `workers.tools.python` | boolean | `true` | Declare Python available to workers. Strict removal requires a different image. |
| `workers.tools.git` | boolean | `true` | Declare Git available to workers. Strict removal requires a different image. |
| `workers.tools.compilers` | boolean | `true` | Declare compiler tools available. Strict removal requires a different image. |
| `workers.tools.package_install` | boolean | `false` | Declare package installation permitted; image and network policy still apply. |
| `workers.tools.communication` | `none`, `controller_summary`, `island`, or `global` | `none` | Previous-attempt information injected into later worker prompts. |

`web_search` and Claude Code's `shell` setting alter the actual harness command.
The Python, Git, compiler, and package-install fields describe the declared
experiment capability; they cannot remove a binary already present in an image.
Use separate worker images for strict binary/tool ablation studies. The built-in
Codex adapter rejects `shell: false` rather than silently running a shell-enabled
agent.

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

## Workflow components

The workflow is three ordered component lists, not a DAG or a configuration
language. Built-ins cover the normal experiment. Owner code may supply trusted
components through Python config helpers; YAML resolves only names registered by
the package.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `workflow.phases[].name` | registered name | `prepare`, `explore`, `validate`, `hidden`, `report` | Ordered run-level phases with declared artifact requirements and outputs. Core phases are mandatory in the alpha. |
| `workflow.phases[].options` | mapping | `{}` | Constructor options for one phase. |
| `workflow.attempt_steps[].name` | registered name | `mutate`, `static_audit`, `smoke`, `public_evaluate`, `feedback` | Ordered steps executed for each candidate attempt. `mutate` and `public_evaluate` are mandatory. |
| `workflow.attempt_steps[].options` | mapping | `{}` | Constructor options for one attempt step. |
| `workflow.after_iteration[].name` | registered name | `migration` | Policies evaluated after the iteration barrier. They propose edits; only the engine applies them. |
| `workflow.after_iteration[].options` | mapping | `{}` | Constructor options for one after-iteration policy. |

Use immutable config helpers such as `without_step(...)`, `with_step(...)`,
`with_phase(...)`, `without_phase(...)`, `with_phase_options(...)`, and
`with_evaluator(...)` to define ablations without editing a shared config in
place. For example, one matrix can derive scout `off`, `shared`, and
`per_island` variants with `with_phase_options`. Removing the `static_audit` step changes reported pipeline evidence, but
does not remove the engine's mandatory final safety and edit-scope gate.

`PopulationEdit.budget` is reserved but not implemented in the alpha. A custom
policy returning a nonempty budget edit fails explicitly; it is not silently
recorded as an effective scheduling ablation.

The built-in full research sequence is:

```text
prepare, direction_scout, explore, strategy_analysis, recombine,
validate, hidden, challenger, report
```

Its phase options live under `workflow.phases[].options` and are validated by
the phase constructor before a run directory is created:

| Phase | Option | Default | Meaning |
|---|---|---:|---|
| `direction_scout` | `mode` | `shared` | `off`, `shared`, or `per_island`. |
| `direction_scout` | `guided_islands` | first half | Island indexes receiving scout cards. |
| `direction_scout` | `max_directions` | `4` | Maximum direction cards. |
| `direction_scout` | `worker_index` | `0` | Entry in `workers.pool`. |
| `direction_scout` | `timeout_seconds` | worker default | Role wall time. |
| `direction_scout` | `token_budget` | worker default | Advisory role token budget. |
| `direction_scout` | `max_budget_usd` | worker default | Claude role cost cap. |
| `direction_scout` | `tools` | network/web on | Role-specific `ToolConfig` overrides. |
| `direction_scout` | `prompt_version` | `direction-scout/1` | Recorded prompt contract. |
| `strategy_analysis` | `max_strategies` | `6` | Cards normalized per island. |
| `strategy_analysis` | `max_ablations` | `6` | Executable toggles tested per island. |
| `strategy_analysis` | `min_effect` | `0.01` | Public-fitness support threshold. |
| `strategy_analysis` | `n_repeats` | `1` | Repeated ablated evaluations. |
| `strategy_analysis` | `worker_index` | `0` | Entry in `workers.pool`. |
| `strategy_analysis` | `timeout_seconds` | worker default | Role wall time. |
| `strategy_analysis` | `token_budget` | worker default | Advisory role token budget. |
| `strategy_analysis` | `max_budget_usd` | worker default | Claude role cost cap. |
| `strategy_analysis` | `tools` | network on, web off | Role-specific `ToolConfig` overrides. Remote CLI harnesses need network transport to their model API. |
| `strategy_analysis` | `prompt_version` | `strategy-analysis/1` | Recorded prompt contract. |
| `recombine` | `max_strategies` | `8` | Portable strategies considered. |
| `recombine` | `max_combination_size` | `2` | Patches in one combination. |
| `recombine` | `max_combinations` | `12` | Hard combination evaluation cap. |
| `recombine` | `beam_width` | `3` | Validation-selected combinations registered. |
| `challenger` | `reference` | `scipy_powell` | `scipy_powell`, `prima_newuoa`, or `initial`. This phase is post-selection only. |

See [Research workflow](research-workflow.md) for artifact schemas, fallback
behavior, patch portability, and validation-query semantics.

## Provider configuration

The package passes model names, environment variables, profiles, and extra
arguments to Codex or Claude Code. It does not maintain a vendor registry. A
compatible provider can be configured in one worker entry. Compatibility means
that the endpoint implements the selected CLI's API dialect and tool calling,
not merely that it accepts a model string. For Claude Code, map host-side
package variables to the names expected by the CLI:

```yaml
workers:
  pool:
    - harness: claude
      model: ${OPTIPROFILER_EVOLVE_MODEL}
      env:
        ANTHROPIC_BASE_URL: ${OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL}
        ANTHROPIC_AUTH_TOKEN: ${OPTIPROFILER_EVOLVE_API_KEY}
```

Codex custom providers require the Responses wire protocol. Configure the
provider explicitly rather than treating `OPENAI_BASE_URL` as an analogue of
Claude Code's gateway variable:

```yaml
workers:
  pool:
    - harness: codex
      model: ${OPTIPROFILER_EVOLVE_CODEX_MODEL}
      env:
        CODEX_PROVIDER_API_KEY: ${OPTIPROFILER_EVOLVE_API_KEY}
      args:
        - --config
        - 'model_provider="compatible"'
        - --config
        - 'model_providers.compatible.base_url="${OPTIPROFILER_EVOLVE_OPENAI_BASE_URL}"'
        - --config
        - 'model_providers.compatible.env_key="CODEX_PROVIDER_API_KEY"'
        - --config
        - 'model_providers.compatible.wire_api="responses"'
```

Use the environment-variable names and model identifier expected by the
installed CLI/provider. Provider support should be verified with that CLI
before starting a multi-iteration experiment. See
[Model providers and agent workers](providers.md) and the checked-in compatible
provider examples for complete small runs.
