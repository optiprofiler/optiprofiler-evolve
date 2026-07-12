# Security Model

## Enforced in the MVP

- The original solver and sibling OptiProfiler repositories are never mounted
  into coding-worker containers.
- Each worker gets one copied workspace, two public evaluation capabilities,
  bounded call quotas, CPU/memory/PID limits, no Linux capabilities, a read-only
  container root, and no Docker socket.
- Hidden problem names and the immutable reference live only under the
  controller directory.
- Public evaluation requests use a private file queue. The controller does not
  expose a host port and does not write evaluation output through
  worker-controlled paths.
- Canonical evaluation runs after the worker exits and rejects symbolic links,
  special files, excessive tree size, invalid interfaces, and edits outside the
  declared scope.
- The evaluator is a separate no-network Docker container. Candidate and
  immutable reference mounts are read-only.

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
