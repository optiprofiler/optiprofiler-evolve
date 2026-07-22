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
- The built-in `explore` phase owns the iteration/island/attempt barrier loop.
- A candidate attempt is an ordered list of steps.
- After-iteration policies read one immutable snapshot and propose bounded
  `PopulationEdit` values.
- The configuration is not a general DAG, compiler, or embedded language.

## 3. State ownership

- The engine is the sole writer of populations, accepted candidate records,
  checkpoints, and authoritative event ordering.
- Steps produce `StepResult`; policies produce `PopulationEdit`; neither may
  mutate controller state directly.
- A failed component cannot leave a partially accepted candidate.
- Validation and hidden results cannot enter a worker prompt. Controller-only
  validation may rank and retain candidates; hidden evaluates one
  validation-selected champion once and never affects the evolution loop.

## 4. Extension contracts

The supported extension slots are `Phase`, `AttemptStep`,
`AfterIterationPolicy`, `RetentionPolicy`, `ParentSampler`, `WorkerAdapter`, and
`Evaluator`.

- A trusted custom component can be supplied directly without editing framework
  files.
- A package built-in needs its implementation, one explicit registry entry, and
  tests. It must not require an `engine.py` branch.
- Registries are plain dictionaries. No auto-discovery or package entry-point
  framework is introduced before an external plugin actually requires it.
- Process executors and Docker mechanics remain internal in v1. Harbor may be a
  future worker backend, but it is not a core dependency or public abstraction.
- `WorkerAdapter` receives a controller-prepared private trace directory and a
  cooperative cancellation event. It may add native evidence there, but it
  cannot replace controller-owned input/outcome manifests. A transcript-only
  adapter is supported with an explicitly degraded fallback trace.
- Trusted phases receive one frozen `ControllerServices` value capped at five
  operations: run a sanitized role agent, materialize a gated variant, evaluate
  a public variant, select IDs by controller validation, and register a finalist.
  Adding a sixth operation requires a deliberate constitution change and tests.
- A research phase must not become a second engine. It may propose file-backed
  artifacts and call these services; only the engine writes candidate and
  population state.
- Retention and parent sampling are read-only proposals over one bounded island
  archive. They cannot admit candidates, change lineage, exceed capacity, or
  bypass controller validation.

## 5. Trust and isolation

- Owner-installed extension code is trusted. Coding workers and candidate code
  are untrusted within the documented alpha threat model.
- Attempt contexts expose public capabilities, never evaluator objects,
  population state, validation/hidden splits, or the full config.
- Scout and analyst roles receive sanitized file copies and no evaluation
  broker. Validation selection returns IDs, never validation values.
- Workers cannot mount OptiProfiler/problem-library source, the immutable
  reference, hidden data, Docker socket, host home, or sibling repositories.
- The engine's final edit-scope, tree, interface, and candidate-safety gate is
  mandatory. An ablation may remove an evidence-producing audit step, not this
  gate.

## 6. Reproducibility

Every run records:

- the canonical redacted resolved config and its hash;
- package, Python, platform, component identity, component source hashes, worker
  CLI versions when available, and evaluator/worker image identity;
- the frozen data manifest and immutable reference;
- coordinate-derived random seeds using `(seed, phase, iteration, island,
  attempt, component)`;
- candidate snapshots, lineage, checkpoints, evaluation artifacts, and worker
  transcripts.
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
- Status values are `pending`, `running`, `succeeded`, `failed`, `skipped`, or
  `cancelled`.
- `public_run_state.json`, `status.html`, shareable reports, GitHub Actions
  summaries, and future public formats consume only the sanitized event
  projection. They never become competing state stores.
- The built-in local status page is static and server-free. GitHub Actions is an
  optional launcher/exporter, not the runtime scheduler.

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
