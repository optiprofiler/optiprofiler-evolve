# Architecture

## Main flow

```text
evolve(...)
  -> prepare
       freeze config, provenance, solver, and data split
       evaluate the seed on public and controller-only validation sets
  -> explore
       for each iteration
         launch configured attempts for every island
         run each private attempt pipeline
           mutate -> audit -> smoke -> public evaluate -> feedback
         run enabled formal criteria, including integrity review by default
         controller evaluates enabled performance criteria on validation
         wait at the iteration barrier
         engine accepts/rejects and ranks each island by its formal weighted score
         after-iteration policies propose migration/budget/stop edits
         engine applies accepted proposals and checkpoints state
  -> validate
       select one champion using controller-only validation
  -> hidden
       evaluate that fixed champion once on the holdout
  -> report
       materialize final_solver and private scientific results
       project public events into status.html, PUBLIC_REPORT.md, and public/
```

The optional research harness keeps these core actions and inserts:

```text
prepare -> direction_scout -> explore -> strategy_analysis -> recombine
        -> validate -> hidden -> challenger -> report
```

`direction_scout` and `strategy_analysis` run read-only research roles in
sanitized workspaces without evaluation tools. They produce versioned files.
The engine-owned `ControllerServices` materializes and gates executable variants,
runs public evaluation, performs opaque validation selection, and registers only
selected finalists. `recombine` uses bounded portable-patch combinations;
`challenger` is post-selection reporting and cannot alter the champion.

The public vocabulary is: **run > phase > iteration > island > attempt >
step**. An iteration is a synchronized round over all islands. An island may
launch more than one attempt through `attempts_per_island`.

## Extension layers

The package has workflow slots, equal-authority quality criteria, selection
policies, and backend adapters:

| Contract | Purpose | May change population? |
|---|---|---|
| `Phase` | One ordered run-level stage with named `requires` and `provides` artifacts | Only through an engine-owned core operation |
| `AttemptStep` | Mutate, inspect, or evaluate one private candidate attempt | No |
| `AfterIterationPolicy` | Read an immutable population snapshot and propose kill/migrate/budget/stop edits | No; the engine applies proposals |
| `RetentionPolicy` | Choose the ordered bounded archive retained by one island | No; the engine validates and applies the choice |
| `ParentSampler` | Choose one parent from an island archive | No |
| `CandidateReviewer` | Approve or quarantine a public-gated tree before validation | No |
| `Criterion` | Produce a score and constraint verdict under one assessment profile | No |
| `WorkerAdapter` | Launch a coding worker and collect lifecycle output | No |
| `Evaluator` | Execute trusted public/validation/hidden benchmark calls | No |

Components are resolved from small explicit dictionaries. There is no automatic
plugin discovery, DAG compiler, or arbitrary configuration language. A trusted
owner extension may be supplied directly in Python. Package entry points should
be added only after a real external plugin package needs them.

Every model invocation enters `agents/llm.py` through
`LLMClient.run(AgentJob) -> AgentJobResult`. The controller first binds a
`WorkerRequest` containing the selected provider, private workspace, tools,
broker, resource limits and cancellation event. The client supplies the job
prompt, creates trace inputs, invokes the injected `WorkerAdapter` once,
finalizes evidence, and collects declared outputs. Job text cannot select a
different host workspace or grant additional capabilities.

Solver proposal/edit tasks use the `SolverJob` subtype. At this same LLM
entrypoint, each enabled criterion contributes its own generation guidance from
allowlisted public rule fields. Profile-specific weights and constraints stay
explicit; hidden settings and private evaluation evidence are never inputs.
Review jobs remain separate and receive no generation guidance. Neither the
exploration stage nor research role preparation contains criterion-name
branches or special-case prompt concatenation.

Built-in CLI workers retain the real Codex/Claude tool loop and exact native
streams. A transcript-only custom adapter remains supported with explicitly
degraded trace timing. Explorer attempts, research roles and integrity reviewers
use the same client; pure preparation, selection, holdout and reporting do not
acquire model calls. Research modules retain the bounded
`ControllerServices.run_trusted_agent` capability, whose implementation prepares
the isolated job and calls the client. They never receive a backend or raw
subprocess launcher. Stage prompts and interpretation remain with their stages;
the client owns invocation mechanics, not scientific decisions.

Each terminal invocation is joined to run/config/component provenance in
`controller/trace_index.jsonl`. The separate
`controller/trace_coverage.json` reports capture quality, worker outcomes, and
provider-gateway outcomes; `public_trace_coverage.json` contains aggregate
counts only.

`events.jsonl` remains the only authoritative workflow ledger.
`public_events.jsonl`, `public_run_state.json`, `status.html`,
`PUBLIC_REPORT.md`, and the exact `public/` bundle are derived views. The local
page renders the canonical run > phase > iteration > island > attempt > step
vocabulary. The GitHub Actions template launches one engine job and displays
the same public projection; it does not compile the workflow into GitHub jobs.

The `budget` field is reserved for a future scheduler. The alpha engine raises
on a nonempty budget proposal so an apparently successful experiment cannot
silently ignore a budget policy.

Quality checks share `AssessmentRunner`, which applies configured weights and
warn/enforce constraints without admitting candidates itself. Public tools,
seed preparation, ordinary submissions and research variants use the same
criteria. The fixed seed equals its parent and has no mutation transcript, so
its integrity review is a controller-local baseline approval that never invokes
the reviewer agent; only actual submissions spend reviewer budget. Integrity
review can be disabled like any other criterion; an outage is failed evidence,
never a fabricated quarantine. Review retries and private
evidence are owned by the integrity implementation rather than the engine.

## State ownership

Source complexity uses a bounded static-analysis subprocess with the initial
solver as its fixed baseline. It preserves raw benchmark fitness and contributes
its own independently weighted score. Worker tools assess one private snapshot
and expose only public-profile feedback. Profile overrides may change budget,
weights and constraints; formal candidate scores always share one configuration.

The controller owns population state, candidate acceptance, checkpoints, and the
event ledger. Its helpers under `controller/` commit phase results and research
variants on its behalf; no phase owns a second population store. Attempt steps
return immutable `StepResult` values. Policies return immutable `PopulationEdit`
proposals.

Every core and optional stage has its own class in `phases/<name>.py`.
Its specific context and result types live in the same module.
The core classes contain the actual stage algorithms rather than dispatching
back to an engine method. Prepare, Validate and Hidden return typed
staged results; the controller commits them after checking the declared
artifacts. Explore owns the iteration barrier and receives immutable population
snapshots plus named admission, policy and checkpoint callbacks. Core inputs
are frozen stage-specific contexts; optional phases retain the smaller shared
`PhaseContext`. Neither receives the engine itself or a generic state setter.
Report returns only its report artifact; application builds the public return
value even when report is omitted.

The attempt context contains only its coordinate, private workspace, parent
summary, prior step results, public capabilities, and event emitter. It does not
contain population objects, validation/hidden data, the evaluator, or the full
experiment config.

Candidate records retain their origin island as immutable lineage. In an
`IterationView`, current island membership is the index of the outer
`populations` tuple; migration deliberately does not rewrite origin metadata.

Retention and parent sampling are separate registered components. Their exact
identity and options are recorded in provenance. The default components are
golden-tested against the former in-engine implementation; research variants do
not silently change the baseline experiment.

Mandatory edit-scope and candidate-safety checks remain in the engine even when
the optional `static_audit` evidence step is removed for an ablation.

## Module boundaries

| Module | Responsibility |
|---|---|
| `api.py` | The single public `evolve(...)` function |
| `config.py`, `models.py`, `solver.py`, `data.py`, `files.py`, `errors.py` | Cross-subsystem configuration, domain values, solver/data boundaries and filesystem primitives |
| `application/run.py` | Compose adapters and controller services; own run startup, traces and infrastructure cleanup |
| `application/experiments.py`, `provenance.py` | Orchestrate experiment matrices with an injected run callable; resolve component identities for provenance |
| `controller/coordinator.py`, `phase_contexts.py` | Ordered phase execution and registered, bounded context factories |
| `controller/state.py`, `commits.py` | Explicit run state and the sole candidate/population/artifact commit operations |
| `controller/runtime.py` | Frozen controller inputs and execution ports; no application dependency |
| `phases/attempts/` | Private attempt execution, public-only capabilities, prompts, steps and iteration policy proposals |
| `controller/variants.py`, `research_evaluation.py`, `validation.py`, `finalists.py`, `role_jobs.py` | Separate variant, evaluation, selection, registration and role capabilities |
| `observability/live_views.py` | Live projection scheduling and rendering; no population authority |
| `controller/registry.py`, `builtins.py`, `selection.py` | Explicit registration and archive selection |
| `phases/<name>.py` | One class per core or optional stage, including its specific inputs/results and local helpers |
| `phases/contracts.py`, `_support.py`, `_research.py` | Stage contracts, shared phase helpers and research artifact formats |
| `agents/` | Unified LLM invocation, CLI backend, isolation, provider transport, public tool broker and feedback |
| `criteria/` | Criterion contracts, runner, aggregator, and performance/complexity/integrity implementations |
| `criteria/performance/` | Performance criterion, canonical evaluator, subprocess entrypoint, references and benchmark evidence |
| `observability/` | Events, provenance, private traces, usage, report evidence validation and report generation |
| `observability/views/` | Public/private derived views and their evidence readers, refreshed throughout a run |

The former `quality` package is named `criteria`. The mixed `evaluation` package
has been dissolved into performance execution and agent tools. There is no
catch-all `shared` package: agent contracts live in `agents/contracts.py`, phase
contracts in `phases/contracts.py`, controller capabilities in
`controller/contracts.py`, and evaluator contracts beside the performance
implementation. A module's owner is determined by its responsibility, not by
how many consumers import it.

No module copies or modifies OptiProfiler source. `optiprofiler` is a normal
runtime dependency and remains a separate package.

Package initializers do not register components or launch work. Internal import
paths changed with this layout and have no compatibility forwarding modules.
The public `evolve` API and documented `serve`/`docker_gc` command entrypoints
remain unchanged. Provenance records the new component module names and source
hashes honestly; experiment configuration is unchanged. Complexity reports use
`source_complexity/2`: `score` is the criterion score and `complexity_percent`
is the baseline-relative percentage.

### Dependency and state rules

`api -> application -> controller coordinator -> phases -> bounded services` is
the execution path. Agents, criteria and observability implement the execution
ports. Lower layers never import application; the coordinator does not import
Docker, LLM clients, brokers, analyzers, reviewers or HTML implementations.
Criteria receive resolved guidance classes and reviewer factories instead of
looking them up in controller registries. Application supplies component
identities and prompt versions to provenance; observability only captures them.
Enabled criterion factories are resolved once before startup and shared by
assessment, generation guidance and provenance. The controller binds the assessor
factory for Prepare; the phase supplies only its newly prepared seed, reference
and data, without constructing controller implementations.
See the [dependency rules](architecture-constitution.md#10-dependency-direction).

`RunState` owns the run's registries and committed phase results. Core result
constructors derive artifacts from their typed fields; state artifacts and
champion/final accessors are read-only views of these results, not separate stores.
`RunCommits` is the only production writer. Ordinary attempts and research
finalists retain immutable candidate records and submit them through this same
authority. Admission and research qualification remain distinct operations:
their gates differ, so neither is forced through a fictitious linear status chain.

Each phase sees only its `requires` artifacts and available `optional_requires`
artifacts. Optional producers may be absent, but configured producers must precede
their consumers; scout directions are an example. Context factories are registered mappings,
not action dispatchers. Failed/cancelled phase results are never committed;
successful and skipped phases must publish exactly their declared outputs.

The default five-phase order is unchanged, but configured workflows may contain
any ordered list, including no phases. After constructing components, preflight
in `phases/contracts.py` checks unique resolved phase names, unique artifact producers,
and inputs against preceding outputs.
Optional `validate_config(config)` hooks check phase-specific configuration using
the constructed options. No directory or run infrastructure exists yet. The
coordinator does not reorder phases or invent missing producers.

Ordinary custom phases need only the `Phase` protocol and `PhaseResult`; they do
not need controller registration or new state classes. Core context factories
and typed commit branches remain explicit because they grant authority that an
artifact alone cannot grant. Factories bind to implementation types (including
subclasses), not stage names; an unrelated custom `report` gets the ordinary
context. The shared `prepared` artifact is a frozen
`PreparedArtifact` with typed paths and solver metadata, not an evaluator or
data-split handle. See [custom workflows](custom-workflows.md) for an example.

One `RunState` persists throughout a run. Once validation fixes the champion,
controller commit and service entries reject further admission, population edits,
finalist registration and reselection. Entry checks avoid expensive work; commit
checks protect authoritative writes. Hidden checks the fixed champion's tree
hash and consumes a single-use authorization before evaluation, even if it fails.
An exclusively created `controller/hidden_started.json` records the champion ID
and tree hash before execution; an existing marker forbids another query, even
if the in-memory flag was lost. Later checkpoints also include `hidden_started`.
This is durable consumption evidence, not support for resuming interrupted runs.
Removing hidden is allowed unless a champion-returning run requires a lockbox.
Post-selection public challenger comparisons remain evidence-only.

Application returns `EvolveResult` after any successful workflow. It never guesses
a winner from a seed or population: an unselected champion means nullable winner
and score fields. A selected champion without hidden retains its candidate path
and formal assessment, but has no final score or finalists. Hidden's exported
solver takes precedence when present. `artifacts` is a detached top-level
read-only mapping, not a deep state snapshot; exceptions and cancellation are
not converted into partial success.
The final integrity check hashes the path actually returned, including the export
after any optional post-selection phases. One lockbox policy checks planned core
implementations at assembly and committed typed results at publication; ordinary
artifact names cannot satisfy it.

File formats and paths remain owned by their subsystems. This refactor does not
add generic path dispatch, wrapper registries around dictionaries, another
research directory, or a second event schema beside the existing ledger.

## Isolation

Evaluators retain numerical results and sanitized benchmark files. Only the
worker feedback projection writes `feedback.md` and `artifact_index.json`; the
private evaluator output does not contain a duplicate presentation bundle.
Seed, admitted candidate and research finalist records point to controller-built
feedback bundles. Scout and analyst inputs consume these same projections as the
worker broker; they never fall back to a private evaluation directory or an
unmatched last broker call. Transfer jobs use `SolverJob` and receive the same
criterion guidance as exploration jobs.
With full-artifact feedback, the final public candidate evaluation retains
configured benchmark artifacts, so the analyst receives evidence for the actual
admitted tree without another benchmark call.
Complexity analysis caches only source measurements. Each criterion invocation
applies its profile policy once and persists `complexity/complexity.json` beside
the assessment, so diagnostic and admission evidence describe the same rules.
Integrity completion events record profile-specific decision paths. Owner pages
and evidence archives consume those paths, including fingerprinted review runs,
rather than reconstructing directories from attempt IDs.
`CandidateAssessment` names the composite assessment port; `Evaluator` names only
the numerical backend. Criteria bind their dependencies through `Criterion.bind`
and return evidence through their declared contract. Raw benchmark output is
captured by the controller's benchmark callback, not read from a concrete
criterion's mutable attributes. Research role preparation receives explicit
worker, sandbox and trace dependencies without the shared run environment.

### EvalReport evidence

`observability/eval_reports.py` reads the private `optiprofiler.eval_report/1`
and `optiprofiler.plot_data/1` pair. Its sibling `schemas/` resources are exact
copies pinned to Core revision `bf9d61c1482da6f0ad46fab660270cc8484d9f6f`,
verified offline by digest. This does not certify that every supported installed
OptiProfiler version produces that protocol. `jsonschema` is a runtime dependency.

The reader owns file containment, non-symlink regular files and bounded reads;
`_eval_report_protocol.py` owns schema, receipt, reference and numeric-shape rules.
Default limits are 16 MiB for the main
report and 256 MiB for the companion. The caller authorizes the main report's
parent; companion paths cannot escape it or traverse symlinks beneath it.
The reader does not authorize other file paths mentioned in the report, nor act
as an OS sandbox against concurrent mutation of controller-owned directories.

`load_eval_report` returns an `EvalReportBundle` or raises `EvalReportError`.
Unrecognized versions raise its `UnsupportedEvalReport` subclass. Queries by
history, plot or target-work ID return borrowed records that callers must not
mutate; unknown IDs raise `KeyError`. Numerical/scoring completion is
required; rendering failure alone does not erase valid numerical evidence.
Validated evidence is not proof of convergence, comparability, reproducibility,
or permission to publish.

This is reader support, not a new evaluation path: fitness still comes from
`benchmark()` return values. No phase, scorer or research agent automatically
consumes these reports, and `agent_report.json` is not treated as an alias.

Before full worker artifact copying, feedback discovers standard-named
`eval_report.json` files at the output root and beneath `benchmark/`, and checks
only their version and companion receipt paths, not the full numerical report, to exclude
both the main files and their contained companion paths, including custom
companion names. Root receipts matter because their companions may reside in
`benchmark/`. Missing, malformed or unsupported receipts withhold additional
artifacts before any benchmark file is copied, but preserve the evaluation's
score and success status. The controller retains `feedback_warning.json` and
emits a private `feedback_artifacts_withheld` event; the worker sees only a generic
notice. Nonstandard main-report names are not discovered;
future producer integration must supply explicit report paths and a separately
allowlisted public projection. Raw report files are never added to the public
bundle allowlist.

### Worker boundaries

Workers receive a private copy of one candidate, an anonymous public manifest,
and brokered `smoke_test`/`evaluate` commands. They do not receive validation or
hidden manifests, the fixed reference source, OptiProfiler/problem-library
source, Docker socket, host home, or sibling repositories.

Gateway-routed Docker workers receive only a dummy credential and an internal
sidecar address. The worker stays on an invocation-only internal network; only
the sidecar joins a separate egress network. Its route, credential, audit, and
terminal evidence remain controller-owned.

The engine records `attempt_id` across worker transcripts, step events, candidate
lineage, and benchmark artifacts. Reproducible component seeds are derived from
the complete coordinate `(run seed, phase, iteration, island, attempt,
component)` so removing one optional component does not silently shift unrelated
random streams.

Validation-profile assessment is controller-only, not end-only. Its weighted
total drives default retention, sampling and champion selection. Raw public,
validation and hidden fitness are separate evidence and may be absent when
performance is disabled. Hidden remains a one-time final check and never feeds
back. See [Algorithm assessment](assessment.md) for weights and failure behavior.

## Repository candidates

`initial` may be a single file or a directory. A directory is the complete
solver implementation. The interface uses `relative/path.py:function`; Python
solver-internal imports should be relative so candidate and reference modules
can coexist during one benchmark call.

Workers may add, delete, or reorganize files matched by `editable`. The engine
snapshots the resulting complete tree, never a model-generated diff, and never
modifies the original input.

The normative rules are in the
[architecture constitution](architecture-constitution.md).
