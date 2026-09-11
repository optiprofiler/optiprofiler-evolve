# Architecture Constitution

This document defines the stable boundaries of the alpha architecture. New
features and experiments must preserve these rules unless this document is
changed deliberately with matching tests.

## 1. Public surface

- `from optiprofiler_evolve import evolve` remains the only top-level operation.
- Internal protocol and config classes are not alpha compatibility promises.
- The canonical vocabulary is run, phase, iteration, island, attempt, and step.

## 2. Control plane

- The outer workflow is an ordered list of phases with explicit named
  `requires` and `provides` artifacts.
- The default is `prepare -> explore -> validate -> hidden -> report`, not a
  mandatory phase list. Custom lists may be empty. Preflight checks dependencies
  in supplied order, unique resolved names and optional phase configuration hooks
  before creating a run directory or infrastructure; it never sorts the list.
- Available `optional_requires` inputs are passed without requiring a producer.
  If a producer is configured, it must precede its optional consumer.
  Successful and skipped phases must return exactly their declared outputs.
- The built-in `explore` phase owns the iteration/island/attempt barrier loop.
- A candidate attempt is an ordered list of steps.
- After-iteration policies read one immutable snapshot and propose bounded
  `PopulationEdit` values.
- The configuration is not a general DAG, compiler, or embedded language.

## 3. State ownership

- `RunCommits` is the sole writer of `RunState`, including populations, accepted
  candidate records, research registries and checkpoints. The coordinator orders
  phases; application assembles infrastructure and the authoritative event sink.
- Controller-owned variant helpers submit through that commit authority;
  this is one authority, not a separate database per stage. Core stages return
  typed results or call named controller commit operations. They never receive
  the engine or an arbitrary state mutation interface.
- Steps produce `StepResult`; policies produce `PopulationEdit`; neither may
  mutate controller state directly.
- A failed component cannot leave a partially accepted candidate.
- Validation and hidden results cannot enter a worker prompt. Controller-only
  validation may rank and retain candidates; hidden evaluates one
  validation-selected champion once and never affects the evolution loop.
- Champion selection closes admission, finalist registration and reselection.
  The controller checks the fixed champion's source hash before
  authorizing hidden, and consumes that authorization before evaluation. Failure
  cannot authorize a retry. A required lockbox needs hidden performance evaluation.
- One `RunState` persists across phases; there are no per-phase state subclasses
  or full-system snapshots. `prepared` is a frozen phase-owned data artifact.
  Core result artifacts derive from their typed fields. Run state retains these
  committed results, with read-only artifact and champion/final views rather than
  separately writable copies. Optional phase extensions and defaults are defined
  together in `phases/contracts.py`; assembly validates their dependencies.
- Application constructs `EvolveResult` after successful workflow completion.
  Without a champion, winner and score fields are `None`; without hidden, final
  fields are absent (`None`/empty tuple). Report only produces report evidence.
  Returned artifacts are a detached, top-level read-only mapping, not a deep
  snapshot or a resume format. Exceptions and cancellation still propagate.

## 4. Extension contracts

The supported extension slots are `Phase`, `AttemptStep`,
`AfterIterationPolicy`, `RetentionPolicy`, `ParentSampler`, `CandidateReviewer`,
`WorkerAdapter`, `Evaluator`, and `Criterion`.

- A trusted custom component can be supplied directly without editing framework
  files.
- A package built-in needs its implementation, one explicit registry entry, and
  tests. It must not require a coordinator branch.
- Each core and optional phase has a concrete class in `phases/<name>.py`,
  alongside its specific context and result types. Helpers belong to their
  owning subsystem, not a catch-all shared package. A directory must represent
  a real multi-module subsystem, not merely wrap one class or one file.
  Core context factories are registered by the controller; optional extensions
  use `PhaseContext` and the existing five capabilities. There is no string
  action dispatcher or engine mixin hierarchy behind the phase classes.
- Registries are plain dictionaries. No auto-discovery or package entry-point
  framework is introduced before an external plugin actually requires it.
- Process executors and Docker mechanics remain internal in v1. Harbor may be a
  future worker backend, but it is not a core dependency or public abstraction.
- `WorkerAdapter` receives a controller-prepared private trace directory and a
  cooperative cancellation event. It may add native evidence there, but it
  cannot replace controller-owned input/outcome manifests. A transcript-only
  adapter is supported with an explicitly degraded fallback trace.
- `LLMClient.run(AgentJob)` is the sole backend invocation entrypoint for
  mutation, research and review. Its controller-bound request fixes workspace,
  provider, tool and broker authority. The client owns trace lifecycle and
  declared output collection, but never candidate admission or phase scheduling.
  Pure evaluation phases do not invoke a model.
- Solver proposal/edit tasks use `SolverJob`. The LLM entrypoint combines their
  task with criterion-owned generation guidance, using controller-bound public
  rule fields only. Review jobs never inherit those instructions. Each
  `Criterion` owns both its assessment and its generation guidance; stages must
  not hard-code standard-specific instructions or append special-case notes.
- Trusted phases receive one frozen `ControllerServices` value capped at five
  operations: run a sanitized role agent, materialize a gated variant, evaluate
  a public variant, select IDs by controller validation, and register a finalist.
  Adding a sixth operation requires a deliberate constitution change and tests.
- `ControllerServices.run_trusted_agent` is the only agent-invocation capability
  exposed to workflow modules. Modules never receive a worker adapter or a raw
  subprocess launcher. Every future agent-invoking module therefore inherits
  the same controller-owned trace lifecycle by construction.
- A research phase must not become a second engine. It may propose file-backed
  artifacts and call these services; only the engine writes candidate and
  population state.
- Retention and parent sampling are read-only proposals over one bounded island
  archive. They cannot admit candidates, change lineage, exceed capacity, or
  bypass controller validation.
- Quality criteria have equal authority: performance, complexity and integrity
  can each be disabled, weighted, or enforced. A shared runner applies their
  verdicts; only the controller admits candidates. Formal assessment requires
  an enabled positive-weight criterion. Disabled checks never fabricate scores
  or approval, and execution safety boundaries remain mandatory.

## 5. Trust and isolation

- Owner-installed extension code is trusted. Coding workers and candidate code
  are untrusted within the documented alpha threat model.
- Attempt contexts expose public capabilities, never evaluator objects,
  population state, validation/hidden splits, or the full config.
- Scout and analyst roles receive sanitized file copies and no evaluation
  broker. Validation selection returns IDs, never validation values.
- Integrity reviewers receive only sanitized parent/candidate trees, edit
  metadata, and the mutation transcript. They receive no evaluator, problem
  manifest, population state, validation/hidden data, or network access.
- Workers cannot mount OptiProfiler/problem-library source, the immutable
  reference, hidden data, Docker socket, host home, or sibling repositories.
- Gateway-routed Docker workers receive neither the real provider credential
  nor the provider origin. They stay on an invocation-only internal network;
  only the short-lived sidecar may join its separate egress network.
- The engine's final edit-scope, tree, interface, and candidate-safety gate is
  mandatory. An ablation may remove an evidence-producing audit step, not this
  gate.

## 6. Reproducibility

Every run records config, provenance, events and trace coverage. When the
corresponding stages execute, it also records:

- the canonical redacted resolved config and its hash;
- package, Python, platform, component identity, component source hashes, worker
  CLI versions when available, and evaluator/worker image identity;
- the frozen data manifest and immutable reference;
- coordinate-derived random seeds using `(seed, phase, iteration, island,
  attempt, component)`;
- candidate snapshots, lineage, checkpoints, evaluation artifacts, and worker
  transcripts;
- normalized integrity decisions and complete traces for every reviewer attempt;
- provider-gateway ready/audit/outcome evidence and Docker lifecycle/cleanup
  evidence for every gateway-routed invocation;
- a private run-level trace index, capture-quality/outcome coverage, and stable
  joins from each invocation to its workflow, candidate lineage, config, and
  run provenance;
- research prompt-template versions, direction/strategy schemas, variant base
  and patch hashes, validation query counts, and rejected materializations.

Removing one optional component must not shift the random stream of unrelated
components.

## 7. Events and views

- `events.jsonl` is append-only and has one writer. Every event has a sequence,
  timestamp, kind, scope, status, and data payload.
- `events.jsonl` is controller-private. A centralized kind-and-field allowlist
  derives `public_events.jsonl`; unknown kinds and fields are withheld by
  default. No renderer or exporter performs its own ad hoc redaction.
- `attempt_id` joins step events, worker transcripts, candidate records, and
  evaluation artifacts.
- Provider-delivered stdout and stderr are retained separately and incrementally
  in controller-private per-invocation directories. Readable transcripts are
  derived evidence, never replacements for the raw streams. See
  [Agent trace retention](traces.md).
- Every invocation has an input manifest and a terminal outcome manifest. A
  timeout or SIGINT/SIGTERM cancellation terminates the complete worker process
  group on POSIX; incomplete invocations are marked by an idempotent crash
  scanner rather than silently treated as complete.
- `controller/trace_index.jsonl` contains one idempotent terminal row per agent
  invocation. `controller/trace_coverage.json` distinguishes complete,
  degraded, and interrupted capture from worker success, failure, timeout, and
  cancellation. Public projections expose aggregate counts only.
- Status values are `pending`, `running`, `succeeded`, `failed`, `skipped`, or
  `cancelled`.
- `public_run_state.json`, `status.html`, shareable reports, GitHub Actions
  summaries, and future public formats consume only the sanitized event
  projection. They never become competing state stores.
- The built-in local status page is static and server-free. GitHub Actions is an
  optional launcher/exporter, not the runtime scheduler.
- The complete run directory is private. GitHub Actions and other exporters may
  upload only the controller-generated `public/` directory, whose exact
  fixed-name allowlist is tested. They never upload `run_dir`, `FINAL_REPORT.md`,
  controller state, raw traces, candidates, or workspaces.

## 8. Ablations

- Ablation variants are pure immutable `EvolveConfig -> EvolveConfig`
  transformations.
- A matrix run records each fully resolved config, its difference from the base,
  seed, result, and failure. It writes the summary atomically after every cell so
  one failed seed cannot erase completed work.
- Safety boundaries, hidden-set secrecy, and event/provenance recording cannot be
  disabled as experimental factors.
- One-shot alternatives should be included when a claimed iterative mechanism
  is being evaluated.
- `Observed` strategy evidence requires a successfully materialized and
  evaluated executable toggle. Agent prose alone cannot upgrade evidence.

## 9. Complexity budget

Do not introduce a god context, nested override mini-language, auto-discovery
framework, public executor hierarchy, or second authoritative database. A new
abstraction must remove demonstrated duplication or enable a concrete ablation
that the current five extension contracts cannot express.
