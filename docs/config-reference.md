# Configuration Reference

The YAML config controls one fixed experiment. It is strict: unknown keys and
invalid combinations fail before workers start. The checked-in
[JSON Schema](../config.schema.json) provides editor completion and structural
validation; Python validation in `config.py` remains authoritative for
cross-field rules.
Tests validate serialized Python presets against this schema as well as YAML
examples. Reviewer component defaults may be omitted; an empty worker pool is
rejected before reviewer resolution.

Defaults shown below are package defaults. Calling `evolve(..., config=None)`
uses the same bounded quick-start values: one iteration, one island, one
attempt, and no whole-candidate migration. Fields marked **required** have no
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
| `data.split.lockbox_required` | boolean | `false` | Fail before evolution or final publication when no nonempty hidden split can be evaluated. Enable for paper-grade runs. |
| `data.split.public` | list of strings | `[]` | Explicit worker-facing training set. A nonempty value enables explicit splitting. |
| `data.split.validation` | list of strings | `[]` | Explicit controller-only selection set. |
| `data.split.hidden` | list of strings | `[]` | Explicit controller-only final holdout set. |
| `data.split.smoke` | list of strings | `[]` | Optional explicit fast subset; it must be contained in `public`. |

Explicit public, validation, and hidden lists must be pairwise disjoint and
together contain the entire selected universe. Smoke is always a public subset.
Workers receive only the two evaluation capabilities and an anonymous count/hash
manifest; names, aliases, validation results, and hidden results remain in the
controller directory.

## Assessment

See [Algorithm assessment](assessment.md) for scoring, privacy and migration.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `assessment.criteria` | mapping of named criteria | performance and integrity enabled, complexity disabled | Complete criterion map when supplied; omitted standards do not run. |
| `assessment.profiles` | mapping | `{}` | One-level overrides for smoke, public, validation and hidden. Integrity defaults to validation only. |
| `assessment.criteria.<name>.enabled` | boolean | `true` | Master switch; disabled standards allocate no backend resources. |
| `assessment.criteria.<name>.weight` | finite nonnegative number | `1` | Independent contribution to the weighted average; zero means constraints/diagnostics only. |
| `assessment.criteria.<name>.mode` | warn or enforce | enforce; complexity default warn | Whether an unmet constraint blocks acceptance. |
| `assessment.criteria.<name>.options` | mapping | `{}` | Standard-specific settings, overridden by key rather than recursively. |

Scores may be negative or exceed one; no framework clipping or normalization is
applied. Formal assessment must have an enabled, positive-weight standard.
Missing weighted evidence makes the total unavailable, never zero. Raw
performance and weighted totals are stored separately.

## Complexity

Set `assessment.criteria.complexity.enabled: true` to enable this standard.
It preserves raw benchmark fitness but contributes independently to the total.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `assessment.criteria.complexity.mode` | `warn` or `enforce` | `warn` | Advise simplification, or block over-budget/unavailable candidates before evaluation and admission. |
| `assessment.criteria.complexity.options.max_ratio` | finite positive number | `1.2` | Maximum candidate-to-initial ratio for both decision complexity and logical code lines. Equality passes. |

The baseline is the frozen initial solver, never the current parent or benchmark
reference. Radon 6.0.1 counts decisions across all candidate Python files and
lexical scopes, with one base point for the complete tree. Logical lines exclude
comments, blanks and actual docstrings. Existing snapshot exclusions apply;
test-named directories are not specially excluded. Installed dependencies are
outside this source budget. The analyzer score is `1 / (1 + max(ratios))`, matching
the criterion score; `complexity_percent` is 100 times the larger ratio;
the branch denominator is the initial decision complexity and the size
denominator is `max(initial logical lines, 1)`.

Both worker tools return complexity diagnostics in every feedback mode. Enforced
rejections consume the normal call quota and return no invented fitness score.
The worker may edit and resubmit. Analysis is static, cached by source hash and
run-fixed metric/analyzer versions, and limited to 30 seconds per subprocess.
Positive-weight analysis failures make the total unavailable even in warn mode;
zero-weight failures follow warn/enforce. Baseline failures abort preparation.
Source changes invalidate previous results. Research variants use the same
assessment profiles. An enforced ratio below one rejects the initial solver
during preparation; use a soft score to search for strict simplifications.

Detailed reports remain under `controller/complexity/`; worker feedback contains
only numeric baseline totals and candidate diagnostics. Public events expose
summary numbers and statuses only. These are source complexity heuristics, not
algorithmic runtime complexity or proof against dynamically generated code.

## Evaluation

| Field | Type | Default | Meaning |
|---|---|---|---|
| `assessment.criteria.performance.options.benchmark` | mapping | `{}` | Options forwarded to `optiprofiler.benchmark`; data and solver identity fields are controller-owned. |
| `assessment.criteria.performance.options.smoke_overrides` | mapping | `{}` | Benchmark options overlaid only for `smoke_test`. |
| `execution.backend` | `docker` or `unsafe_local` | `docker` | Execution boundary for Python candidate code. `unsafe_local` executes untrusted solver code in the controller process and is for trusted development only. |
| `execution.docker_image` | string or `null` | `null` | Evaluator image; **required** when backend is `docker`. |
| `execution.timeout_seconds` | positive integer | `3600` | Wall-time limit for one Docker evaluator process; `unsafe_local` has no process timeout. |
| `execution.cpus` | positive number | `2.0` | CPU limit passed to the evaluator container; ignored by `unsafe_local`. |
| `execution.memory` | Docker memory string | `4g` | Evaluator-container memory limit; ignored by `unsafe_local`. |
| `execution.pids_limit` | integer at least `16` | `512` | Evaluator-container process limit; ignored by `unsafe_local`. |
| `execution.feedback_mode` | `score_only` or `full_artifacts` | `score_only` | Worker-visible projection of controller-owned benchmark evidence. `full_artifacts` also copies allowlisted benchmark artifacts and an artifact index. |
| `assessment.criteria.performance.options.reference` | registered competitor name | `initial` | Fixed reference solver paired with every candidate benchmark. Use `initial` for seed-relative evolution; reserve a strong solver for post-selection comparison. |
| `assessment.criteria.performance.options.fitness_source` | `solver_scores` or `profile_scores_mean` | `solver_scores` | Population fitness source. `profile_scores_mean` computes each entry as `(candidate-reference+1)/2`, clips it to `[0, 1]`, and averages the complete OptiProfiler profile tensor. |
| `execution.forbidden_candidate_imports` | list of dotted Python module names | `[]` | Reject candidate source that uses these imports. This is an auditable experiment ablation, not a security boundary. |
| `execution.max_smoke_calls_per_worker` | nonnegative integer | `20` | Broker quota for worker `smoke_test` calls. |
| `execution.max_public_calls_per_worker` | nonnegative integer | `5` | Broker quota for worker `evaluate` calls. |
| `execution.adapter` | registered name | `optiprofiler` | Trusted evaluator adapter. Change this only for an owner-supplied evaluator implementation. |

Evaluation quotas are local to one worker job and each invocation consumes one
slot, including a failed execution. An exhausted quota is non-retryable;
waiting does not restore it.

`profile_scores_mean` writes `fitness_breakdown.json` beside each execution.
The file records the normalized tensor, its axis meanings, and the exact mean
used for retention. It changes only the controller-owned fitness reduction;
workers still receive the configured public feedback and never see validation
or hidden problem identities.

When a post-selection challenger such as `prima_newuoa` is installed in the
evaluator image, add its import root (`prima`) to
`execution.forbidden_candidate_imports` if candidates must remain independent
of that implementation. Apply the same rule to any other installed solver API
excluded by the experiment contract, such as `scipy.optimize`. This check makes
the policy auditable; it is not a sandbox security boundary.

`load` and `solvers_to_load` are rejected in both benchmark mappings. Candidate
and the fixed reference solver must be executed together in each canonical
benchmark call. The controller overrides `plibs`, `problem_names`, solver names,
normalization, and other identity-sensitive fields.

### Built-in competitor registry

The Python evaluator can materialize these trusted reference wrappers. Optional
packages are imported inside the evaluator only; choosing PDFO or PRIMA does
not make either package a mandatory dependency of the controller.
The checked-in evaluator image includes SciPy, PDFO, and independent PRIMA. It
pins NumPy 1.26 because the current PDFO wheel is not compatible with NumPy 2.
Custom evaluator images must provide the package named by the selected
competitor; a missing runtime fails on its first evaluator call instead of
silently falling back to another solver.

| Family | Unconstrained `u` | Bounds `b` | Linear `l` | Nonlinear `n` |
|---|---|---|---|---|
| SciPy | `scipy_nelder_mead`, `scipy_powell`, `scipy_cobyla`, `scipy_cobyqa` | `scipy_powell`, `scipy_cobyla`, `scipy_cobyqa` | `scipy_cobyla`, `scipy_cobyqa` | `scipy_cobyla`, `scipy_cobyqa` |
| PDFO | `pdfo`, `pdfo_uobyqa`, `pdfo_newuoa`, `pdfo_bobyqa`, `pdfo_lincoa`, `pdfo_cobyla` | `pdfo`, `pdfo_bobyqa`, `pdfo_lincoa`, `pdfo_cobyla` | `pdfo`, `pdfo_lincoa`, `pdfo_cobyla` | `pdfo`, `pdfo_cobyla` |
| independent PRIMA | `prima`, `prima_uobyqa`, `prima_newuoa`, `prima_bobyqa`, `prima_lincoa`, `prima_cobyla` | `prima`, `prima_bobyqa`, `prima_lincoa`, `prima_cobyla` | `prima`, `prima_lincoa`, `prima_cobyla` | `prima`, `prima_cobyla` |

Every wrapper accepts the four OptiProfiler solver arities (`u`, `b`, `l`,
`n`), checks method compatibility before evaluation, translates bounds and
constraints, and applies the experiment's evaluation budget. The registry is
also used by the post-selection challenger phase, so reference and challenger
names cannot drift apart.

## Evolution

| Field | Type | Default | Meaning |
|---|---|---|---|
| `evolution.iterations` | positive integer | `1` | Number of synchronized population iterations. |
| `evolution.islands` | positive integer | `1` | Number of independently maintained populations. |
| `evolution.attempts_per_island` | positive integer | `1` | Candidate attempts launched for each island in one iteration. |
| `evolution.population_per_island` | positive integer | `2` | Maximum retained candidates in each island. |
| `evolution.migration_interval` | nonnegative integer | `0` | Whole-candidate ring-migration interval; `0` disables migration. |
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
| `workers.prompt_note` | non-empty string (≤2000 chars) or `null` | `null` | Extra run-owner work constraint rendered into the worker task card as `Experiment note (must follow)`. It shapes how workers spend the budget; it never replaces isolation, evaluation policy, or safety limits. |
| `workers.adapter` | registered name | `cli` | Worker lifecycle adapter. The built-in adapter runs Codex or Claude Code. `provider_gateway` currently requires `cli`; custom adapters do not implement that credential boundary. |
| `workers.allow_direct_provider` | boolean | `false` | First unsafe opt-in for bypassing the provider gateway. Docker direct mode also requires `sandbox.allow_direct_network=true`. |
| `workers.pool[].harness` | `claude` or `codex` | **required** | Coding-agent CLI adapter. |
| `workers.pool[].model` | string | **required** | Model identifier passed unchanged to the selected CLI. |
| `workers.pool[].weight` | positive integer | `1` | Relative probability of selecting this worker entry. |
| `workers.pool[].profile` | string or `null` | `null` | Optional Codex profile name. |
| `workers.pool[].args` | list of strings | `[]` | Extra CLI arguments appended to the harness command. |
| `workers.pool[].env` | string mapping | `{}` | Explicit environment values or `${ENV_NAME}` references passed to the worker. |
| `workers.pool[].pass_env` | list of names | `[]` | Host environment variables copied into the worker; missing names fail early. |
| `workers.pool[].provider_gateway.upstream_base_url` | HTTPS base URL | **required for gateway mode** | Controller-owned provider origin. It is never supplied to the worker. A base path such as `/v1` or `/anthropic` is allowed. |
| `workers.pool[].provider_gateway.credential_env` | environment name | **required for gateway mode** | Name of the one resolved credential delivered to the gateway sidecar, not the worker. |
| `workers.pool[].provider_gateway.protocol` | `auto`, `anthropic`, or `openai_responses` | `auto` | Fixed wire protocol. `auto` maps Claude to Anthropic Messages and Codex to OpenAI Responses. |
| `workers.pool[].provider_gateway.auth_mode` | `auto`, `bearer`, or `x-api-key` | `auto` | Upstream credential header. Auto uses `x-api-key` for Anthropic API keys and Bearer otherwise. |
| `workers.pool[].provider_gateway.max_request_bytes` | positive integer | `16000000` | Maximum worker-to-gateway request body. Responses are streamed and bounded by timeout/token policy, not buffered to this size. |
| `workers.pool[].provider_gateway.connect_timeout_seconds` | positive integer | `15` | Upstream connection timeout. |
| `workers.pool[].provider_gateway.response_timeout_seconds` | positive integer | `900` | Idle timeout while receiving the provider response stream. |

Do not store credentials directly in YAML. Prefer `pass_env` or an
`${ENV_NAME}` reference. Keys containing common credential markers are redacted
from `resolved_config.json`.

When `provider_gateway` is configured, provider routing is controller-owned.
Claude receives only the internal gateway URL and a dummy API key. Codex
receives a generated Responses provider definition and ignores user config.
Conflicting provider arguments, provider profiles, and base-URL environment
variables fail before launch. Unrelated secret or `pass_env` values also fail
rather than entering a worker container.

## Integrity review

The semantic integrity criterion is enabled by default for formal assessment,
after public checks and before validation execution. It can be disabled or
configured as a warning like any other criterion. The default implementation uses a
separate coding-agent invocation with no evaluation broker, network, problem
manifest, population, validation data, or hidden data. It sees sanitized copies
of the parent, candidate, changed-file list, solver contract, and mutation
transcript. The fixed seed equals its parent and has no mutation transcript, so
its integrity check is a controller-local baseline approval that never invokes
the reviewer; only actual submissions consume reviewer budget. After configured
retries, reviewer unavailability is failed evidence for that submission, never
a fabricated approve or quarantine verdict.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `assessment.criteria.integrity.options.component.name` | registered reviewer name | `agent_integrity` | Reviewer implementation. `unsafe_approve` is restricted to explicit tests/ablations. |
| `assessment.criteria.integrity.options.component.options` | object | `{}` | Constructor options for an owner-supplied reviewer. |
| `assessment.criteria.integrity.options.worker` | worker mapping or `null` | `null` | Dedicated reviewer worker. Required unless same-model reuse is explicitly allowed. |
| `assessment.criteria.integrity.options.worker.harness` | `claude` or `codex` | inherited only when worker is `null` | Dedicated reviewer CLI. |
| `assessment.criteria.integrity.options.worker.model` | string | inherited only when worker is `null` | Dedicated reviewer model identifier. |
| `assessment.criteria.integrity.options.worker.weight` | positive integer | `1` | Scheduling weight; retained for the shared worker schema. |
| `assessment.criteria.integrity.options.worker.profile` | string or `null` | `null` | Codex profile; incompatible with gateway routing. |
| `assessment.criteria.integrity.options.worker.args` | list of strings | `[]` | Extra non-provider CLI arguments. |
| `assessment.criteria.integrity.options.worker.env` | string mapping | `{}` | Explicit reviewer environment values. |
| `assessment.criteria.integrity.options.worker.pass_env` | list of names | `[]` | Reviewer host variables resolved by the controller. |
| `assessment.criteria.integrity.options.worker.provider_gateway.upstream_base_url` | HTTPS base URL | **required for gateway mode** | Same pinned provider route as a mutation worker; may point to a different provider. |
| `assessment.criteria.integrity.options.worker.provider_gateway.credential_env` | environment name | **required for gateway mode** | Reviewer gateway credential name. |
| `assessment.criteria.integrity.options.worker.provider_gateway.protocol` | `auto`, `anthropic`, or `openai_responses` | `auto` | Reviewer wire protocol. |
| `assessment.criteria.integrity.options.worker.provider_gateway.auth_mode` | `auto`, `bearer`, or `x-api-key` | `auto` | Reviewer upstream authentication mode. |
| `assessment.criteria.integrity.options.worker.provider_gateway.max_request_bytes` | positive integer | `16000000` | Reviewer request-body limit. |
| `assessment.criteria.integrity.options.worker.provider_gateway.connect_timeout_seconds` | positive integer | `15` | Reviewer upstream connection timeout. |
| `assessment.criteria.integrity.options.worker.provider_gateway.response_timeout_seconds` | positive integer | `900` | Reviewer response-stream idle timeout. |
| `assessment.criteria.integrity.options.allow_same_model` | boolean | `false` | Permit reuse of the first mutation worker identity. This is an explicit ablation, not the recommended research setup. |
| `assessment.criteria.integrity.options.allow_unsafe_stub` | boolean | `false` | Required second opt-in when `component.name` is `unsafe_approve`. |
| `assessment.criteria.integrity.options.retries` | nonnegative integer | `1` | Additional attempts after malformed output or reviewer unavailability. |
| `assessment.criteria.integrity.options.timeout_seconds` | positive integer | `600` | Wall-time limit for one reviewer invocation. |
| `assessment.criteria.integrity.options.token_budget` | positive integer or `null` | `4000` | Reviewer-specific advisory token budget. |
| `assessment.criteria.integrity.options.max_budget_usd` | positive number or `null` | `null` | Reviewer-specific Claude Code cost cap. |

The nested `assessment.criteria.integrity.options.worker` accepts the same fields as
`workers.pool[]`, including `provider_gateway`. When `allow_same_model` is false,
its harness/model identity must not
appear in the mutation pool. Every review attempt retains a complete private
trace under `research/traces/integrity-reviewer/`.

## Worker tools

| Field | Type | Default | Meaning |
|---|---|---|---|
| `workers.tools.preset` | `minimal`, `research`, or `custom` | `minimal` | Named tool policy recorded for the experiment. |
| `workers.tools.web_search` | boolean | `false` | Expose harness-native web-search tools when supported. Requires `network: true`; gateway mode carries provider-side search without granting shell egress. |
| `workers.tools.network` | boolean | `true` | Permit network-backed harness tools. Gateway-routed workers remain internal; direct worker egress exists only in the double-opt-in unsafe path. |
| `workers.tools.shell` | boolean | `true` | Include `Bash` in Claude Code's tool list. The built-in Codex adapter cannot enforce `false` and rejects that combination. |
| `workers.tools.python` | boolean | `true` | Declare Python available to workers. Strict removal requires a different image. |
| `workers.tools.git` | boolean | `true` | Declare Git available to workers. Strict removal requires a different image. |
| `workers.tools.compilers` | boolean | `false` | Declare compiler tools available. Strict removal requires a different image. |
| `workers.tools.package_install` | boolean | `false` | Declare package installation permitted; image and network policy still apply. |
| `workers.tools.communication` | `none`, `controller_summary`, `island`, or `global` | `none` | Previous-attempt information injected into later worker prompts. |

`web_search` and Claude Code's `shell` setting alter the actual harness command.
When communication is enabled, the controller includes only bounded public
fitness metadata plus the normalized solver summary, mechanism names, and
pseudocode. It never forwards validation/hidden values, reviewer findings, or
raw explanation paths.
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
| `sandbox.gateway_image` | string | `optiprofiler-evolve-gateway:latest` | Minimal stdlib-only provider gateway image. It receives no solver, broker, evaluator, or host-home mount. |
| `sandbox.cpus` | positive number | `2.0` | CPU limit for one worker container. |
| `sandbox.memory` | Docker memory string | `4g` | Worker-container memory limit. |
| `sandbox.pids_limit` | integer at least `16` | `512` | Worker-container process limit. |
| `sandbox.gateway_cpus` | positive number | `0.5` | CPU limit for one provider gateway sidecar. |
| `sandbox.gateway_memory` | Docker memory string | `256m` | Memory limit for one provider gateway sidecar. |
| `sandbox.gateway_pids_limit` | integer at least `16` | `64` | Process limit for one provider gateway sidecar. |
| `sandbox.gateway_start_timeout_seconds` | positive integer | `30` | Maximum wait for the ready manifest and health check before worker launch fails. |
| `sandbox.max_candidate_files` | positive integer | `2000` | Maximum files accepted from one worker workspace. |
| `sandbox.max_candidate_bytes` | positive integer | `200000000` | Maximum total bytes accepted from one worker workspace. |
| `sandbox.allow_direct_network` | boolean | `false` | Second unsafe opt-in for a Docker worker to bypass the provider gateway and use direct egress. |

Gateway mode requires Docker Engine 28 or newer for deterministic gateway
priority across the sidecar's internal and egress networks.

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
| `workflow.phases[].name` | registered name | `prepare`, `explore`, `validate`, `hidden`, `report` | Ordered run-level phases with declared inputs/outputs. The list may be empty; required inputs and configured optional producers must precede consumers. |
| `workflow.phases[].options` | mapping | `{}` | Constructor options for one phase. |
| `workflow.attempt_steps[].name` | registered name | `mutate`, `smoke`, `public_evaluate` | Ordered candidate-attempt steps. Explore validates mandatory `mutate` and `public_evaluate` steps before startup. Without an ExplorePhase, names are not resolved and steps are not constructed; defaults or an empty list are both valid. |
| `workflow.attempt_steps[].options` | mapping | `{}` | Constructor options for one attempt step. |
| `workflow.after_iteration[].name` | registered name | none | Optional policies evaluated after the iteration barrier. They propose edits; only the engine applies them. |
| `workflow.after_iteration[].options` | mapping | `{}` | Constructor options for one after-iteration policy. |

Use immutable config helpers such as `without_step(...)`, `with_step(...)`,
`with_phase(...)`, `without_phase(...)`, `with_phase_options(...)`, and
`with_evaluator(...)` to define ablations without editing a shared config in
place. For example, one matrix can derive scout `off`, `shared`, and
`per_island` variants with `with_phase_options`. Removing the `static_audit` step changes reported pipeline evidence, but
does not remove the engine's mandatory final safety and edit-scope gate.

`without_phase` may remove a built-in stage too. This does not remove its
dependents: deleting prepare while retaining explore fails preflight. Empty and
custom-only lists are supported without report or attempt steps. Champion
selection still closes admission; a required lockbox cannot be omitted or have
hidden performance disabled in a champion-returning run. See
[custom workflows](custom-workflows.md) for return semantics and extension code.

`PopulationEdit.budget` is reserved but not implemented in the alpha. A custom
policy returning a nonempty budget edit fails explicitly; it is not silently
recorded as an effective scheduling ablation.

The recommended full research sequence is:

```text
prepare, direction_scout, explore, strategy_analysis, strategy_transfer,
validate, hidden, challenger, report
```

Its phase options live under `workflow.phases[].options` and are validated by
the phase constructor and optional `validate_config(config)` hook before a run
directory is created:

| Phase | Option | Default | Meaning |
|---|---|---:|---|
| `direction_scout` | `mode` | `shared` | `off`, `shared`, or `per_island`. |
| `direction_scout` | `guided_islands` | first half | Island indexes receiving scout cards. |
| `direction_scout` | `max_directions` | `4` | Maximum direction cards. |
| `direction_scout` | `worker_index` | `0` | Entry in `workers.pool`. |
| `direction_scout` | `timeout_seconds` | worker default | Role wall time. |
| `direction_scout` | `token_budget` | worker default | Advisory role token budget. |
| `direction_scout` | `max_budget_usd` | worker default | Claude role cost cap. |
| `direction_scout` | `tools` | network/web on | Role-specific `ToolConfig` overrides. Native search may use the provider tool loop; the role shell remains internal. |
| `direction_scout` | `prompt_version` | `direction-scout/1` | Recorded prompt contract. |
| `strategy_analysis` | `max_strategies` | `6` | Cards normalized per island. |
| `strategy_analysis` | `max_ablations` | `6` | Executable toggles tested per island. |
| `strategy_analysis` | `min_effect` | `0.01` | Public-fitness support threshold. |
| `strategy_analysis` | `n_repeats` | `1` | Repeated ablated evaluations. |
| `strategy_analysis` | `evaluation_mode` | `smoke` | `smoke` for bounded attribution or `public` for canonical public attribution. |
| `strategy_analysis` | `worker_index` | `0` | Entry in `workers.pool`. |
| `strategy_analysis` | `timeout_seconds` | worker default | Role wall time. |
| `strategy_analysis` | `token_budget` | worker default | Advisory role token budget. |
| `strategy_analysis` | `max_budget_usd` | worker default | Claude role cost cap. |
| `strategy_analysis` | `tools` | network on, web off | Role-specific `ToolConfig` overrides. Model transport uses the gateway; the role shell has no general egress. |
| `strategy_analysis` | `prompt_version` | `strategy-analysis/1` | Recorded prompt contract. |
| `strategy_transfer` | `max_strategies_per_island` | `3` | Controller-supported semantic strategies offered to each other island. |
| `strategy_transfer` | `attempts_per_island` | `1` | Integration attempts made from each island's own selected base. |
| `strategy_transfer` | `worker_index` | `0` | Integrator worker entry. |
| `strategy_transfer` | `timeout_seconds` | worker default | Integrator wall time. |
| `strategy_transfer` | `token_budget` | worker default | Advisory integrator token budget. |
| `strategy_transfer` | `max_budget_usd` | worker default | Claude integrator cost cap. |
| `strategy_transfer` | `tools` | network on, web off | Role-specific `ToolConfig` overrides. |
| `strategy_transfer` | `prompt_version` | `strategy-transfer/1` | Recorded prompt contract. |
| `challenger` | `reference` | `scipy_powell` | `initial` or any compatible registered competitor. This phase is post-selection only. |

The older `recombine` phase remains available as an advanced compatibility
component for portable-patch experiments, but it is not part of the recommended
workflow. Whole-candidate migration is likewise opt-in. This is a deliberate
causal-attribution default, not a claim that mature island systems universally
disable migration: [OpenEvolve exposes periodic
migration](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/configs/default_config.yaml)
and [FunSearch periodically resets weaker islands from stronger
ones](https://www.nature.com/articles/s41586-023-06924-6).

## Solver explanations

Every mutation and strategy-integration prompt requires
`.optiprofiler_evolve/solver_explanation.json` using schema
`solver_explanation/1`. The controller normalizes and stores it under
`<run_dir>/explanations/`, then removes the metadata directory from executable
candidate source. Missing or malformed prose is recorded but never converted
into numerical evidence and does not by itself discard a valid solver.

The selected champion is accompanied by:

```text
final_solver/
final_solver.diff
FINAL_SOLVER_EXPLANATION.md
```

The final explanation contains the worker's bounded summary and pseudocode,
source-bound mechanisms, known limitations, and only strategy cards that occur
on the winning lineage. A transferred card is explicitly identified as having
been ablated on its source island; the report does not claim an isolated target-
island effect that was never measured.

See [Research workflow](research-workflow.md) for artifact schemas, fallback
behavior, patch portability, and validation-query semantics.

## Provider configuration

Each Docker worker declares one pinned `provider_gateway`. Compatibility still
depends on the selected CLI's wire protocol and tool loop; a model name and base
URL alone are not sufficient. For Claude Code:

```yaml
workers:
  pool:
    - harness: claude
      model: ${OPTIPROFILER_EVOLVE_MODEL}
      env:
        ANTHROPIC_AUTH_TOKEN: ${OPTIPROFILER_EVOLVE_API_KEY}
      provider_gateway:
        upstream_base_url: ${OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL}
        credential_env: ANTHROPIC_AUTH_TOKEN
        auth_mode: bearer
```

Codex custom providers require the Responses wire protocol. The controller
generates the Codex provider definition and pins it to the internal gateway;
do not duplicate `model_provider` arguments in YAML:

```yaml
workers:
  pool:
    - harness: codex
      model: ${OPTIPROFILER_EVOLVE_CODEX_MODEL}
      env:
        CODEX_PROVIDER_API_KEY: ${OPTIPROFILER_EVOLVE_API_KEY}
      provider_gateway:
        upstream_base_url: ${OPTIPROFILER_EVOLVE_OPENAI_BASE_URL}
        credential_env: CODEX_PROVIDER_API_KEY
        auth_mode: bearer
```

The real credential is resolved by the controller and delivered only to the
gateway sidecar. The worker receives a dummy credential and the internal
gateway URL. Direct Docker provider access requires both unsafe opt-ins and is
recorded in provenance. See
[Model providers and agent workers](providers.md) and the checked-in compatible
provider examples for complete small runs.
