# Agent trace retention

Every real agent invocation that crosses the controller's worker-adapter
boundary receives a private trace directory. This includes explorer workers,
integrity reviewers, built-in research roles, and future trusted workflow
modules that call `ControllerServices.run_trusted_agent`. Role names are not a
capture allowlist. A module cannot receive the raw worker adapter, so the
controller boundary remains the only supported way to invoke an agent.

The trace is evidence for later trajectory analysis. It is not a second source
of workflow truth and it is never part of a public run bundle.

## Evidence layers

| Layer | Contents | Intended consumer |
| --- | --- | --- |
| L0 raw | Provider-delivered stdout and stderr bytes, chunk observations, effective prompt, redacted worker/config data, sanitized argv, and join identifiers | Run owner and offline trace tooling |
| L1 derived | A readable transcript reconstructed from L0 | Run owner and analysis agents |
| L2 lifecycle | Ordered controller events such as worker start, finish, timeout, and candidate admission | Controller and private diagnostics |
| L3 public | Default-deny projection of safe lifecycle metadata | Local status page, reports, and GitHub Actions |

L0 is authoritative for what the CLI delivered. L1 can be regenerated and must
not overwrite L0. L2 records what the controller observed and decided; it does
not duplicate the complete provider stream. L3 contains no prompt, model,
provider output, trace path, evaluation secret, or private filesystem path.

## Per-invocation layout

Explorer attempts are stored below `traces/<attempt_id>/`. Integrity reviewers
and research-role agents use `research/traces/<role>/<job_id>/`; reviewer jobs
are uniquely keyed by candidate and retry number. Each directory contains:

```text
input/
  prompt.txt
  resolved_worker.json
  argv.sanitized.json
  input_artifacts.json
raw.stdout.stream
raw.stderr.stream
chunks.jsonl
invocation.json
outcome.json
workspace.json
```

The trace and input directories use mode `0700`; files use `0600`. Output is
drained from stdout and stderr concurrently, flushed after every captured chunk,
and periodically synchronized to disk. On POSIX systems the CLI starts in a new
process group; timeout or controller cancellation terminates the group, so a
descendant cannot keep the trace pipes open indefinitely. A timeout preserves
bytes flushed before termination. `chunks.jsonl` records controller-observed chunk order,
stream, byte offset, length, and monotonic timestamp. It must not be interpreted
as provider-internal token timing or exact ordering inside the child process.

The existing transcript path is reconstructed from the chunk index after the
process exits. Native stdout and stderr remain separate so later ATIF conversion
or provider-specific parsing does not depend on a lossy merged file.

`invocation.json` uses `trace_invocation/2`. It records a unique trace ID, run
and config identity, adapter/harness/model identity, stable workflow join
fields, the input workspace hash, and hashes of controller inputs that existed
before launch. `workspace.json` records the post-invocation workspace hash
without modifying the raw streams. The evidence scanner never follows worker-
created symbolic links or reads special files; it records an unsafe tree state
instead, before the mandatory candidate gate rejects that workspace.
`outcome.json` records terminal state, return code, timeout/cancellation reason,
duration when known, capture/derivation errors, explicit completeness and
truncation flags, and byte counts plus SHA-256 hashes for each raw stream and the
chunk index. Missing outcome manifests can be marked `interrupted`
idempotently by the controller's crash scanner; recovery never invents missing
bytes.

## Run index and coverage

The controller appends one terminal row per invocation to
`controller/trace_index.jsonl`. A `trace_index_entry/1` row contains only
private provenance and integrity metadata: trace/run/config identity, relative
trace path, adapter/harness/model identity, workflow join fields, input/output
tree hashes, terminal outcome, capture quality, and stream byte counts and
hashes. It does not duplicate prompts or provider output.

The index is idempotent by trace ID and safe under concurrent islands. After a
controller interruption, recovery marks unfinished invocations explicitly and
adds only missing index rows. A torn final JSONL row is removed before
reconciliation; complete earlier rows are not rewritten. The per-invocation
manifests remain authoritative, so the index can be reconstructed by scanning
them. Internal recovery tooling calls `trace_ledger.reconcile_trace_run` using
the persisted run provenance; this does not add another public package API.

`controller/trace_coverage.json` separates two concepts:

- capture quality: `complete`, `degraded`, or `interrupted`;
- worker outcome: `completed`, `failed`, `timed_out`, `cancelled`, or
  `interrupted`.

A worker can fail while its trace is complete. Conversely, a successful legacy
adapter can have a degraded trace if it returned only a transcript. The
shareable `public_trace_coverage.json` and `trace_coverage` event contain only
aggregate counts. They omit trace IDs, roles, models, paths, prompts, findings,
and provider content.

Stable join fields include the workflow module, role/job or attempt identity,
candidate and parent identity when applicable, iteration, and island. Trusted
custom role jobs may add scalar `trace_links`; reserved controller identity
fields cannot be replaced.

## Privacy and limitations

- Prompts and raw outputs are controller-private and may contain source code or
  provider content. Do not publish a run directory without an explicit export.
- Environment keys that look like credentials and matching argv values are
  redacted from saved configuration. Credential values are not written into the
  trace manifest.
- The package can preserve only bytes and metadata delivered to or observed by
  the local CLI. It cannot record hidden model reasoning or provider-internal
  state that the provider does not return.
- Built-in CLI adapters preserve native stdout/stderr and chunk timing. An
  owner-supplied adapter runs behind the same controller trace boundary. When it
  returns only the legacy transcript, the controller imports that transcript as
  a private fallback and marks the missing native chunk timing explicitly in
  `outcome.json`.
- SIGINT/SIGTERM requests cooperative cancellation. Built-in CLI workers receive
  it immediately; an owner-supplied adapter must honor the cancellation event in
  `WorkerRequest` to avoid delaying controller shutdown.
- Windows does not provide the POSIX process-group guarantee. Container isolation
  remains the recommended backend for strict descendant cleanup.

Trace retention is mandatory for research runs. Failed, rejected, quarantined,
timed-out, cancelled, retried, and interrupted invocations are retained just as
successful invocations are. Retention duration, compression, and deletion are
explicit owner deployment policies and must not silently truncate or
lossy-compress a live research run. A future exporter must produce a complete
private research bundle separately from a default-deny sanitized public bundle.
Normalized reviewer JSON is stored separately under
`controller/integrity_reviews/<candidate_id>/`; it complements, rather than
replaces, the raw reviewer stream.
The double-opt-in `unsafe_approve` test/ablation component performs no agent
invocation, so it records only the normalized decision and is excluded from
agent-trace coverage counts.
