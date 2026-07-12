# MVP Specification

## Scope

The package evolves one solver repository for one fixed OptiProfiler
problem-library experiment. It is not a generic arbitrary-code evolution
framework.

## Public API

Only `optiprofiler_evolve.evolve` and `__version__` are exported at package
level. Solver location, experiment choices, and worker choices are inputs to
that function, not separate task classes.

## Data contract

At run start the controller resolves the selected problem-library universe and
writes an immutable manifest containing exact problem names and a deterministic
split:

- `smoke`: a small subset of public problems for rapid worker iteration;
- `public`: the complete set used for evolutionary fitness;
- `hidden`: withheld from workers and used only for final reranking.

Smoke must be a subset of public. Public and hidden must be disjoint. Hidden
reranking selects among already fixed finalists; if its result drives later
generations, it is no longer hidden. A paper-grade unbiased estimate requires a
third lockbox/test split that is never used for selection.

## Evaluation contract

Workers receive only `smoke_test` and public `evaluate`. The controller owns the
final public+hidden evaluation. Candidate and immutable initial solver are
passed together to `optiprofiler.benchmark`; normalized fitness is
`(candidate - initial + 1) / 2`.

The complete Python evaluator is implemented. `.m` entrypoints are recognized,
but MATLAB evaluation must fail clearly until its adapter is implemented.

## Isolation contract

- User source is copied and never modified in place.
- Every worker receives a private full-repository workspace.
- Only declared editable paths may enter candidate lineage.
- Worker Docker containers receive no source package, problem library, hidden
  manifest, host home, SSH agent, or Docker socket.
- Evaluation runs in a separate no-network container with candidate and
  reference mounted read-only.
- Broker exchange and published public artifacts live under controller-owned
  run directories, outside the candidate workspace.
- Symlinks, special files, out-of-scope edits, and oversized candidate trees
  are rejected before canonical evaluation.

## Evolution contract

One round dispatches one coding worker per island. Each island keeps a bounded
population selected by canonical public fitness. Champions migrate in a ring at
the configured interval. Workers edit full files in place; there is no
LLM-returned diff protocol.

## Artifact contract

A run preserves the redacted config, full trusted data manifest, worker-visible
manifest, solver contract, candidate lineage, transcripts, evaluation feedback,
checkpoints, fixed finalists, final reranking report, and final solver tree.
