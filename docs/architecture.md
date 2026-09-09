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
         run mandatory independent integrity review
         controller evaluates each surviving candidate on validation
         wait at the iteration barrier
         engine accepts/rejects and ranks each island by validation, then public
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

The package has three workflow slots, a mandatory reviewer slot, two selection
slots, and two backend adapters:

| Contract | Purpose | May change population? |
|---|---|---|
| `Phase` | One ordered run-level stage with named `requires` and `provides` artifacts | Only through an engine-owned core operation |
| `AttemptStep` | Mutate, inspect, or evaluate one private candidate attempt | No |
| `AfterIterationPolicy` | Read an immutable population snapshot and propose kill/migrate/budget/stop edits | No; the engine applies proposals |
| `RetentionPolicy` | Choose the ordered bounded archive retained by one island | No; the engine validates and applies the choice |
| `ParentSampler` | Choose one parent from an island archive | No |
| `CandidateReviewer` | Approve or quarantine a public-gated tree before validation | No |
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

The integrity-review call is not an `AttemptStep`: configuration cannot remove
or reorder it. Its implementation is replaceable, but the engine always calls
it inside `_run_attempt`, after deterministic/public checks and immediately
before the first validation evaluation. Research variants pass the same gate
before controller validation selection. Reviewer failures retry once by
default; exhaustion quarantines the candidate unless strict availability was
requested.

## State ownership

Optional source complexity budgets are computed by `complexity.py` in a bounded
static-analysis subprocess. The initial solver snapshot fixes the baseline.
The controller enforces the same budget for attempts, research variants and
final selection; an optional attempt step cannot disable it. Worker tool calls
use one private snapshot for both complexity and performance analysis and expose
only the sanitized complexity report. Complexity never modifies benchmark fitness.

The engine owns population state, candidate acceptance, checkpoints, and the
event ledger. Its helpers under `controller/` commit phase results and research
variants on its behalf; no phase owns a second population store. Attempt steps
return immutable `StepResult` values. Policies return immutable `PopulationEdit`
proposals.

Every core and optional stage has its own class under `phases/<name>/phase.py`.
The core classes contain the actual stage algorithms rather than dispatching
back to an engine method. Prepare, Validate, Hidden and Report return typed
staged results; the controller commits them after checking the declared
artifacts. Explore owns the iteration barrier and receives immutable population
snapshots plus named admission, policy and checkpoint callbacks. Core inputs
are frozen stage-specific contexts; optional phases retain the smaller shared
`PhaseContext`. Neither receives the engine itself or a generic state setter.

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
| `shared/` | Frozen config, models, protocols, solver/data boundaries and shared evidence contracts |
| `controller/engine.py`, `phase_contexts.py` | Workflow lifecycle, authoritative state and bounded phase bindings/commits |
| `controller/attempts.py`, `attempt_capabilities.py` | Private attempt execution and public-only worker capabilities |
| `controller/services.py`, `role_jobs.py` | Research variant gates and sanitized role preparation |
| `controller/registry.py`, `builtins.py`, `selection.py`, `experiments.py` | Explicit registration, archive selection and experiment matrices |
| `phases/prepare/`, `explore/`, `validate/`, `hidden/`, `report/` | Five core stage classes and their specific inputs/results |
| `phases/direction_scout/`, `strategy_analysis/`, `strategy_transfer/`, `recombine/`, `challenger/` | Optional stage classes and their task prompts/helpers |
| `phases/explore/steps/`, `policies/` | Attempt operations and iteration policy proposals |
| `agents/` | Unified LLM invocation, injected CLI backend, isolation and provider/Docker transport |
| `evaluation/` | Canonical evaluator, evaluator subprocess, references, broker and worker feedback |
| `quality/` | Static complexity analysis and semantic integrity review |
| `observability/` | Events, fingerprints, provenance, private traces and usage |
| `phases/report/reports.py`, `viewers.py`, `owner_views.py` | Final scientific reports and public/private derived views, also refreshed during a run |

No module copies or modifies OptiProfiler source. `optiprofiler` is a normal
runtime dependency and remains a separate package.

Package initializers do not register components or launch work. Internal import
paths changed with this layout and have no compatibility forwarding modules.
The public `evolve` API and documented `serve`/`docker_gc` command entrypoints
remain unchanged. Provenance records the new component module names and source
hashes honestly; artifact schemas and experiment configuration are unchanged.

## Isolation

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

Validation is controller-only, not end-only: each candidate that passes its
public pipeline receives one validation evaluation. Validation is the primary
key for population retention, parent selection, and final champion selection;
public fitness breaks ties. This approximately doubles canonical candidate
evaluation work. Hidden remains a one-time final holdout and never feeds back.

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
