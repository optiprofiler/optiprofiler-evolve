# Custom ordered workflows

The default workflow is `prepare -> explore -> validate -> hidden -> report`.
An owner-supplied workflow may instead contain any ordered list, including zero
phases. There is one run state, not a different state subclass after each phase.

Each ordinary phase defines `name`, `requires`, `provides` and `run(context)`.
It returns `PhaseResult(artifacts=...)`. No inheritance or central context
registration is required. Typed artifact dataclasses belong beside their owning
phase; the built-in `prepared` output is a frozen `PreparedArtifact`.

The coordinator constructs phases and validates their required inputs against
preceding outputs before creating the run directory. Duplicate resolved names
and missing inputs fail immediately. An optional `validate_config(config)` hook
checks phase-specific configuration, using options already applied by the
constructor. `optional_requires` declares inputs that are passed only when
available. Neither optional inputs nor preflight reorder the workflow.

Contexts expose only declared inputs, read-only options, an event emitter and
the existing bounded controller services. Successful and skipped phases must
return exactly their declared outputs. Failed or cancelled results never publish
artifacts; exceptions propagate. A phase failure does not undo already completed
file writes or earlier candidate commits, and checkpoints do not implement resume.

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
of fabricating final scores. Set `attempt_steps: []` when no phase runs attempts;
the built-in explore phase declares `uses_attempts = True` and validates its
mandatory mutate/public-evaluate steps. The attempt entry also enforces those
steps if invoked through a controller extension.

## Authority does not come from artifact names

Ordinary artifacts do not grant candidate-admission or holdout authority. Core
contexts and typed controller commit branches remain explicit; changing that
authority requires matching controller changes and tests, not just adding a
phase. Owner plugins are trusted code, not an in-process security sandbox.

After a champion is selected, controller entries reject new candidates,
population edits, finalist registration and reselection. Hidden checks the fixed
champion identity and source hash, then consumes its one query before execution;
a failure cannot be retried. If `data.split.lockbox_required` is true, a run
returning a champion must perform hidden performance evaluation. Independent
post-selection public challenger comparisons may still produce evidence, but
cannot affect selection. Validation and hidden data must never enter a worker.
