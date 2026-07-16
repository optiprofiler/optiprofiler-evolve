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
- `public`: worker-visible training fitness;
- `validation`: controller-only fitness used to retain and select candidates;
- `hidden`: a final holdout evaluated once on the selected champion.

Smoke must be a subset of public. Public, validation, and hidden are pairwise
disjoint. Hidden results never drive later generations or choose between
candidates.

## Evaluation contract

Workers receive only `smoke_test` and public `evaluate`. The controller owns
validation and hidden evaluation. Candidate and the configured fixed reference
are passed together to `optiprofiler.benchmark`; normalized fitness is
`(candidate - reference + 1) / 2`.

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
population selected by controller-only validation fitness, with public fitness
as a tie-breaker. Champions migrate in a ring at
the configured interval. Workers edit full files in place; there is no
LLM-returned diff protocol.

## Artifact contract

A run preserves the redacted config, full trusted data manifest, worker-visible
manifest, solver contract, candidate lineage, transcripts, evaluation feedback,
checkpoints, validation selection, hidden holdout report, and final solver tree.
