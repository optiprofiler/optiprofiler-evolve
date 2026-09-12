# Numeric solver isolation

Coding workers and independent reviewers use the shared CLI/Harbor runtime.
Executing their output is a different boundary: the Docker evaluator must not
import candidate code into the process that owns benchmark scores or problems.

```text
Host controller (Docker authority)
  trusted scorer container: OptiProfiler + reference + problem provider
    callable numeric proxy: fun / cub / ceq stay in this container
  fresh solver container per benchmark call: candidate + numeric dependencies
    numeric requests and return point only; no network or Docker socket
```

The host starts a fixed command in a fixed image with fixed mounts. A scorer
request cannot choose a host command or path. Pipes connect the containers via
the host; a Unix socket multiplexes benchmark child processes *inside* the
trusted scorer. There are no exposed ports or cross-host Unix socket mounts.
This works with local Docker VMs as well as native Linux Docker.

## Setup

```bash
docker build -f docker/solver/Dockerfile -t optiprofiler-evolve-solver:latest .
docker build -f docker/evaluator/Dockerfile -t optiprofiler-evolve-evaluator:latest .
```

The default solver image contains NumPy and SciPy, not Evolve, OptiProfiler,
providers, or credentials. Extend it for other algorithm dependencies and set
`execution.solver_image`. Custom images are an owner trust decision: do not
reuse an evaluator or worker image containing protected code or credentials.
Missing solver images fail evaluation; they are not pulled during a run and
never trigger fallback to same-process execution. `unsafe_local` remains an
explicitly unsafe option for trusted code only.

## Numerical contract

- The existing positional u/b/l/n signatures are unchanged. Arrays retain
  shape, including empty constraint matrices, and NaN/infinity values.
- Only numeric arrays and opaque oracle positions cross the boundary. No
  callback object, Python pickle, problem identity, score, or artifact is sent.
- Calling `fun` makes exactly one request to the real `fun`; it does not call
  `cub` or `ceq` on the solver's behalf. OptiProfiler owns all evaluation counts,
  histories, features, and stopping rules.
- Standard callback exception types, including `StopIteration`, reach the
  solver so it can catch budget exhaustion and return its best point. Private
  error messages are withheld. Non-builtin custom exceptions become
  `RuntimeError` in the solver. An uncaught callback error is re-raised in the
  scorer with its original type.
- A normal solver exception still uses OptiProfiler's existing failed-solver
  scoring behavior. Transport, launch, missing-image, and outer timeout errors
  fail the evaluation rather than accepting a fallback score.

Frames are capped at 8 MiB and numeric arrays at 200,000 elements and two axes.
Each call has an independent no-network container with a read-only root and
candidate mount, private PID namespace, dropped capabilities, and bounded
memory/PIDs. At most `ceil(execution.cpus)` candidate containers run at once;
each uses at most one CPU. The scorer has its own configured resource limit.
Thus memory is a *per-container* limit, not an aggregate experiment quota.

The whole evaluation timeout also covers queued calls and container startup.
On timeout or cancellation, the host removes the scorer and its candidate
containers. Private `numeric_calls.json` records completion and oracle-call
counts. `evaluator.log` includes bounded runtime diagnostics, capped at 2 MiB.

## Verification

`tests/test_docker_integration.py` exercises the public evaluator API: paired
tie scores, candidate scorer-tampering attempts, u/b/l/n calls, evaluation-budget
exceptions, parallel calls, protected filesystem/network denial, missing-image
failure, and timeout cleanup. Opt in with `OPE_RUN_DOCKER_TESTS=1` after building
the images. Unit tests alone do not certify this runtime boundary.

This is process/container isolation, not a proof against kernel vulnerabilities
or algorithmic benchmark overfitting. Independent review and hidden evaluation
remain separate parts of the workflow.
