# Security Model

## Enforced in the MVP

- The original solver and sibling OptiProfiler repositories are never mounted
  into coding-worker containers.
- Each worker gets one copied workspace, two public evaluation capabilities,
  bounded call quotas, CPU/memory/PID limits, no Linux capabilities, a read-only
  container root, and no Docker socket.
- Validation/hidden names, split membership, alias mapping, and the immutable
  reference live only under the controller directory. Worker-visible benchmark
  files use per-run opaque problem identifiers.
- Public evaluation requests use a private file queue. The controller does not
  expose a host port and does not write evaluation output through
  worker-controlled paths.
- The trusted Docker evaluation request is created in a controller temporary
  directory and mounted as one read-only file into the evaluator. It never
  enters the worker-readable artifact tree.
- Canonical evaluation runs after the worker exits and rejects symbolic links,
  special files, excessive tree size, invalid interfaces, and edits outside the
  declared scope.
- The evaluator is a separate no-network Docker container. Candidate and
  immutable reference mounts are read-only.
- PyCUTEst receives a fresh in-memory cache for each evaluation. That tmpfs is
  executable because CUTEst problems are compiled extension modules; it is not
  mounted from or persisted to the host. The remaining temporary filesystem is
  still `noexec`.
- Evaluator containers pin common numerical-library thread pools to one thread.
  OptiProfiler still controls experiment-level parallelism through `n_jobs`;
  the pin prevents nested BLAS workers and parallel plotting from exhausting
  the container PID budget.

Each attempt receives a distinct workspace, broker token, container, and
temporary Docker network, including workers on the same island. Enabling web
search deliberately gives that private network outbound access; it does not add
host mounts or expose the controller-only data. Set both
`workers.tools.web_search` and `workers.tools.network` to `false` for an
internal, no-egress network.

## Current limitation

The Python candidate function executes in the same interpreter as
`optiprofiler.benchmark` inside the evaluator container. Read-only mounts prevent
persistent modification, but intentionally hostile Python can still inspect or
monkey-patch its evaluation process. The alpha release therefore assumes
research workers optimize the solver rather than attack the scorer.

Before untrusted public execution or paper-grade hidden evaluation, add a
candidate execution protocol with a narrower callback boundary, a static/runtime
policy gate, and adversarial regression tests. Final candidate code should also
be reviewed independently.

When worker network is enabled for remote model APIs or web search, the current
Docker bridge provides outbound access. A controlled egress proxy/allowlist is
required for strict provider-only network experiments.
