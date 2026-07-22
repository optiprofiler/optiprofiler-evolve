# Agent trace retention

Every explorer worker and trusted research-role agent receives a private trace
directory. The trace is evidence for later trajectory analysis; it is not a
second source of workflow truth and it is never part of a public run bundle.

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

Explorer attempts are stored below `traces/<attempt_id>/`. Research-role agents
use `research/traces/<role>/<job_id>/`. Each directory contains:

```text
input/
  prompt.txt
  resolved_worker.json
  argv.sanitized.json
  input_artifacts.json
raw.stdout.stream
raw.stderr.stream
chunks.jsonl
```

The trace and input directories use mode `0700`; files use `0600`. Output is
drained from stdout and stderr concurrently, flushed after every captured chunk,
and periodically synchronized to disk. A timeout preserves bytes flushed before
the process is killed. `chunks.jsonl` records controller-observed chunk order,
stream, byte offset, length, and monotonic timestamp. It must not be interpreted
as provider-internal token timing or exact ordering inside the child process.

The existing transcript path is reconstructed from the chunk index after the
process exits. Native stdout and stderr remain separate so later ATIF conversion
or provider-specific parsing does not depend on a lossy merged file.

## Privacy and limitations

- Prompts and raw outputs are controller-private and may contain source code or
  provider content. Do not publish a run directory without an explicit export.
- Environment keys that look like credentials and matching argv values are
  redacted from saved configuration. Credential values are not written into the
  trace manifest.
- The package can preserve only bytes and metadata delivered to or observed by
  the local CLI. It cannot record hidden model reasoning or provider-internal
  state that the provider does not return.
- The current raw-capture path covers the built-in CLI adapter. Owner-supplied
  worker adapters must return their evidence paths; a controller-side fallback
  and crash-finalization manifest are part of the next stabilization slice.

Trace retention is mandatory for research runs. Retention duration, compression,
and deletion are deployment policies and must not silently truncate a live run.
