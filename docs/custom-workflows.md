# Custom ordered workflows

The default workflow is `prepare -> explore -> validate -> hidden -> report`.
An owner-supplied workflow may instead contain any ordered list, including zero
phases. There is one run state, not a different state subclass after each phase.

Each ordinary phase defines `name`, `requires`, `provides` and `run(context)`.
It returns `PhaseResult(artifacts=...)`. No inheritance or central context
registration is required. Typed artifact dataclasses belong beside their owning
phase; the built-in `prepared` output is a frozen `PreparedArtifact`.

Application constructs phases and `validate_workflow` checks their inputs against
preceding outputs before creating the run directory. Duplicate resolved names
and duplicate artifact producers or missing inputs fail immediately.
An optional `validate_config(config)` hook
checks phase-specific configuration, using options already applied by the
constructor. `optional_requires` declares inputs that are passed only when
available. Their producer may be absent, but a configured producer must precede
its consumer; placing direction scout after explore fails before any model call.
Preflight never reorders the workflow. The optional extension adapters and their
defaults are documented together in `phases/contracts.py`.

Contexts expose only declared inputs, read-only options, an event emitter and
the existing bounded controller services. Successful and skipped phases must
return exactly their declared outputs. Failed or cancelled results never publish
artifacts; exceptions propagate. `PhaseResult(status="failed", error="reason")`
preserves the reason in the raised exception and failure event.
A phase failure does not undo already completed
file writes or earlier candidate commits, and checkpoints do not implement resume.
Core result constructors accept typed fields only and derive their artifacts.
`RunState` stores committed phase results; its artifacts and typed accessors are
read-only views, not additional independently writable registries. `requires`
also declares ordering prerequisites delivered through typed contexts (for
example, hidden's prepared evaluator), not just dictionary values read by a phase.

## Two ordinary phases, no model calls

[`examples/custom_workflow.py`](../examples/custom_workflow.py) inventories source
filenames and writes a count summary. It passes trusted phase instances through
`ComponentConfig.from_component` and replaces `WorkflowConfig.phases`. It needs
neither Docker nor a provider credential:

```bash
python examples/custom_workflow.py --run-dir /tmp/source-inventory-run
```

Use a new run directory each time. The example returns its summary through
`result.artifacts["source_summary"]`; it does not return an optimization winner.
Its local execution configuration is only for trusted metadata processing, not
an isolation recommendation for solver evolution.

## Shortened optimization workflows

| Configured phases | Return value |
|---|---|
| Empty or custom-only | Artifacts, no champion or scores |
| Prepare, explore | Artifacts and populations, no implicit winner |
| Prepare, explore, validate | Champion candidate path and formal scores; no final score or finalists |
| Prepare, explore, validate, hidden | Exported solver and one final result, without requiring report |
| Default five phases | Same final result plus report evidence |

`EvolveResult.artifacts` is a detached, top-level read-only mapping. It is not a
deep immutable snapshot, serialization format, or sanitized public export.

Removing a producer requires removing or replacing its dependents. For example,
report still requires hidden's `final` and `final_solver` outputs. To generate a
pre-holdout summary, supply a separate phase with the appropriate inputs instead
of fabricating final scores. Without an `ExplorePhase` (or subclass), attempt
steps are not constructed; leaving defaults or setting `attempt_steps: []` are
both valid. Unused step names are not resolved. Only `ExploreContext` grants
attempt execution, so there is no general `uses_attempts` flag. Explore validates
mandatory mutate/public-evaluate steps once during assembly, not on each attempt.

## Authority does not come from artifact names

Ordinary artifacts do not grant candidate-admission or holdout authority. Core
contexts and typed controller commit branches remain explicit; changing that
authority requires matching controller changes and tests, not just adding a
phase. Owner plugins are trusted code, not an in-process security sandbox.
Core contexts follow registered implementation classes and their subclasses,
not names. An unrelated custom phase named `prepare` receives `PhaseContext`.
Prepared island metadata must match the configured population count at commit.

After a champion is selected, controller entries reject new candidates,
population edits, finalist registration and reselection. Hidden checks the fixed
champion's source hash, then consumes its one query before execution;
a failure cannot be retried. If `data.split.lockbox_required` is true, a run
returning a champion must perform hidden performance evaluation. Independent
post-selection public challenger comparisons may still produce evidence, but
cannot affect selection. Validation and hidden data must never enter a worker.

Experiment matrix summaries average only successful cells with a final score.
Successful cells without one still count toward `n_ok` and are additionally
counted in `n_unscored`; an entirely unscored variant has `mean_final_score: null`.

Lockbox policy is shared: assembly checks actual `ValidatePhase`/`HiddenPhase`
implementations, and final publication checks their committed typed results.
The latter covers trusted extensions that submit core results without being
built-in phases; naming an ordinary artifact `final` grants no authority. Service
entry guards avoid expensive work after selection closes; commit guards protect
state writes, including proposals made without those service entrypoints.
Publication hashes the exported solver when present, otherwise the selected
candidate, because optional post-selection phases can still modify files.
