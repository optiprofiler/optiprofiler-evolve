# MATLAB Evaluator Design

Status: design only. The current alpha recognizes `.m` entrypoints and fails
before starting workers because a MATLAB evaluator is not implemented.

## Responsibility boundary

The user is responsible for installing MATLAB, satisfying its license, and
installing the MATLAB OptiProfiler package and selected problem libraries. The
evolve package should only need the MATLAB executable and package paths supplied
through experiment configuration.

A MATLAB container is optional. The first adapter should invoke a user-managed
host MATLAB process. Worker sandboxes may remain Docker containers because they
only edit source and call the controller's bounded evaluation tools.

## Runtime routing

The evolution engine remains language-neutral:

```text
interface suffix/runtime
  python -> PythonOptiProfilerEvaluator
  matlab -> MatlabOptiProfilerEvaluator
```

Both adapters must return the same internal `EvaluationResult` and preserve the
same smoke/public/validation/hidden boundaries. Trusted manifests contain problem names and
selection options; the runtime adapter validates that its installed problem
library can load them.

## Fixed reference

The configured reference remains immutable for the complete run. Every candidate
and that same reference solver enter one OptiProfiler `benchmark` call. The MATLAB
adapter must not precompute one score independently because profile scores are
coupled across the solver list.

A future experiment may choose a parent-relative reference, but that is a new
fitness protocol and must not silently change the current semantics.

## Candidate/reference name isolation

Candidate and reference trees usually contain identical entrypoint and helper
names. MATLAB uses process-global path and function/MEX caches, so adding both
trees to one path is incorrect.

The first implementation should use this bounded approach:

1. Start a fresh `matlab -batch` process for every canonical evaluation.
2. Force serial OptiProfiler execution for the first version.
3. Give benchmark two trusted dispatcher handles with distinct names.
4. Before a dispatcher invokes its solver, restore the trusted base path, put
   only that solver tree first, clear symbols previously loaded from either
   solver tree, and rehash the path.
5. Resolve the declared entrypoint and verify `functions(handle).file` is inside
   the expected candidate or reference root.
6. Execute the complete solver call while that path context is active, then
   restore path and working directory with cleanup guards.
7. Track loaded `.m` and MEX files with `inmem('-completenames')`. If a MEX file
   cannot be unloaded safely, fail with a diagnostic instead of risking a mixed
   candidate/reference evaluation.

This supports ordinary multi-file repositories without rewriting user source.
Tests must cover same-named helpers, relative subdirectories, persistent state,
exceptions, and MEX loading before parallel execution is considered.

The stronger long-term boundary is process-isolated trajectory execution plus
profile construction from serialized trajectories. That requires a compatible
OptiProfiler evaluation protocol and is outside this package-only pass.

## Proposed configuration surface

MATLAB-specific process settings should live under the evaluator adapter, not
in `evolve(...)`:

```yaml
evaluation:
  backend: local
  matlab:
    executable: matlab
    optiprofiler_path: /path/to/optiprofiler/matlab
    startup_paths: []
```

Credential or license values must not be persisted by the package. The exact
fields should be added only with the adapter and its integration tests.
