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
       materialize final_solver, result.json, events.jsonl, and status.html
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

Before a `WorkerAdapter` runs, the engine creates its private trace inputs and
passes both `trace_dir` and `cancellation_event` in `WorkerRequest`. Built-in CLI
workers add exact native stream evidence. A custom adapter should do the same;
when it returns only a transcript, the engine imports that file and marks the
missing native timing in the terminal trace manifest. Both explorer attempts
and trusted role jobs pass through the same private engine call site. Workflow
modules can invoke agents only through `ControllerServices.run_trusted_agent`;
they do not receive the adapter or a subprocess capability.

Each terminal invocation is joined to run/config/component provenance in
`controller/trace_index.jsonl`. The separate
`controller/trace_coverage.json` reports capture quality and worker outcomes;
`public_trace_coverage.json` contains aggregate counts only.

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

`engine.py` is the only authoritative writer for population state, candidate
acceptance, checkpoints, and the event ledger. Attempt steps return immutable
`StepResult` values. Policies return immutable `PopulationEdit` proposals.

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
| `config.py`, `presets.py` | Frozen config tree and explicit immutable variants |
| `protocols.py`, `registry.py`, `builtins.py` | Narrow contracts and explicit component lookup |
| `phases/`, `steps/`, `policies/` | Built-in orchestration components |
| `engine.py` | Ordered execution, population ownership, selection, checkpoints, holdout |
| `workers.py`, `harness.py`, `sandbox.py`, `traces.py` | Worker lifecycle, CLI commands, isolation, and per-invocation raw trace capture |
| `trace_ledger.py` | Run-level trace indexing, crash reconciliation, and coverage summaries |
| `evaluation.py`, `broker.py` | Evaluator adapter and bounded public worker tools |
| `solver.py`, `data.py` | Solver tree checks and immutable problem splits |
| `events.py`, `provenance.py`, `viewers.py` | Append-only events, reproducibility evidence, derived local views |
| `selection.py` | Bounded island-archive retention, parent sampling, and Pareto variants |
| `experiments.py` | Repeated seeded ablation matrices over frozen configs |
| `research.py`, `phases/research.py` | Versioned scout/strategy evidence and optional research phases |

No module copies or modifies OptiProfiler source. `optiprofiler` is a normal
runtime dependency and remains a separate package.

## Isolation

Workers receive a private copy of one candidate, an anonymous public manifest,
and brokered `smoke_test`/`evaluate` commands. They do not receive validation or
hidden manifests, the fixed reference source, OptiProfiler/problem-library
source, Docker socket, host home, or sibling repositories.

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
