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
- The worker manifest contains only an opaque experiment identifier, public and
  smoke counts, and a budget note. It does not disclose the problem-library
  adapter, split hash, reference identity, scoring formula, validation score, or
  hidden score.
- Worker evaluation responses expose one fitness value, its direction, problem
  count, and artifact handles. Candidate/reference raw scores remain
  controller-only.
- Public evaluation requests use a private file queue. The controller does not
  expose a host port and does not write evaluation output through
  worker-controlled paths.
- The trusted Docker evaluation request is created in a controller temporary
  directory and mounted into the evaluator as its parent directory, deliberately
  writable rather than read-only: the in-container runner unlinks the request file
  (which carries real problem names) before candidate code can run, and a
  read-only bind would break that scrubbing. The reference is mounted read-only;
  the candidate is not mounted into the scorer at all. Nothing here enters the
  worker-readable artifact tree.
- Canonical evaluation runs after the worker exits and rejects symbolic links,
  special files, excessive tree size, invalid interfaces, and edits outside the
  declared scope.
- The evaluator and each numeric candidate invocation are separate no-network
  Docker containers. Only the scorer can import OptiProfiler, the immutable
  reference, and problem providers. A bounded numeric protocol retains callback
  and scoring authority there; candidates cannot inspect Python callback state.
- PyCUTEst receives a fresh in-memory cache for each evaluation. That tmpfs is
  executable because CUTEst problems are compiled extension modules; it is not
  mounted from or persisted to the host. The remaining temporary filesystem is
  still `noexec`.
- Evaluator containers pin common numerical-library thread pools to one thread.
  OptiProfiler still controls experiment-level parallelism through `n_jobs`;
  the pin prevents nested BLAS workers and parallel plotting from exhausting
  the container PID budget.
- Public evaluation output is first written to a controller-only staging
  directory. Direct occurrences of real problem names in text and paths are
  replaced with opaque IDs. Matching binary artifacts, copied evaluator source,
  and raw pickle/HDF5 state are withheld and counted in
  `redaction_report.json`. The completed directory is then published
  atomically into the worker's read-only artifact mount.
- Unusable EvalReport receipts withhold additional worker artifacts, not the
  completed evaluation score. Broker error responses and evaluation error fields
  use generic messages; exception details and feedback-withholding diagnostics
  remain controller-private.
- The append-only `events.jsonl` ledger is controller-private. Shareable status
  and report files are rebuilt from a centralized default-deny projection;
  unknown event kinds, fields, errors, paths, model identities, validation
  scores, prompts, and traces are withheld automatically.
- Raw worker and role-agent streams, prompts, sanitized invocation metadata, and
  chunk indexes are stored under private per-invocation directories. Public
  projections never expose those paths. See [Agent trace retention](traces.md).
- The private run-level trace index contains role, model, lineage, path, and
  content hashes. The public coverage projection contains aggregate counts
  only; it cannot be used to recover private trace identities or locations.
- On POSIX, timeout and controller cancellation terminate the worker process
  group rather than only the CLI parent. Trace outcome manifests distinguish
  complete, truncated, timed-out, cancelled, and crash-inferred evidence.
- On the default configuration, every candidate that survives public checks is
  reviewed by an isolated semantic integrity agent before any validation query.
  Integrity is an ordinary criterion, so a run can disable it explicitly and the
  review is then skipped. The reviewer has no
  evaluator, problem manifest, validation/hidden data, network, or mutation
  capability. Quarantine prevents population admission.

The reviewer has shell, Python, and Git inside its own worker sandbox so it can
inspect a multi-file solver, but it has no network or evaluation capability.
It is an independent semantic check, not a stronger sandbox or a formal proof
that candidate code is harmless.

Each attempt receives a distinct workspace, broker token, container, and
temporary internal Docker network, including workers on the same island. A
short-lived provider gateway is the only container that also joins a separate
egress network. Enabling native web search permits the selected harness to ask
the pinned provider for that tool; it does not give worker shell commands a
general network route.

## Sharing boundary

The run directory root `status.html` and everything under `owner/` form the
PRIVATE owner console: they embed validation and hidden results, reviewer
findings, worker transcripts, provider details, and links into raw traces.
They are rendered from the raw event ledger for the run owner only and are
never copied into `public/`. The only shareable artifact is the `public/`
bundle, which the controller rebuilds from a fixed source-to-target filename
allowlist fed exclusively by the sanitized public event projection. Owner
evidence packaging (`scripts/pack_owner_evidence.py`) is explicit-only and
refuses symlinks and paths outside the run directory.

## Limits and verification

See [numeric isolation](numeric-isolation.md) for the runtime images, protocol,
resource boundaries, and real-Docker negative controls. `unsafe_local` explicitly
executes trusted candidate code in the evaluator interpreter and provides no
such protection. It is not a fallback for missing Docker images.

Image contents are owner-controlled. A custom numeric image must not contain
benchmark/provider source or credentials. Container isolation is not a proof
against kernel vulnerabilities, benchmark overfitting, or malicious trusted
controller configuration. The reviewer remains an independent semantic gate,
not a formal security proof.

The default gateway path is provider-only and route pinned. Brokered shell
egress and package installation are intentionally unavailable. The optional
direct-provider path gives the worker a regular Docker bridge only after both
unsafe opt-ins are set; do not use it for strict experiments.
