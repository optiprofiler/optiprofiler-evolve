# Optional Harbor backend

Harbor is an environment backend, not a second evolution engine. The default
remains `workers.adapter: cli`. To use Harbor, install on Python 3.12 or newer:

```bash
python -m pip install 'optiprofiler-evolve[harbor]'
docker compose version
```

Use your existing gateway-enabled experiment configuration and change only:

```yaml
workers:
  adapter: harbor
  # Keep pool, provider_gateway and tools from your existing configuration.
sandbox:
  backend: docker
```

The fragment above is not a complete experiment. Start with
[the compatible-provider example](../examples/experiment-claude-compatible.yaml)
or [the Codex example](../examples/experiment-codex-compatible.yaml).
Harbor 0.23.0 is pinned and checked at adapter creation. Missing prerequisites
fail explicitly; there is no fallback to unsandboxed local execution. Existing
CLI-only installations still support Python 3.11.

## Ownership

```text
Evolve workflow / admission / scoring
  -> LLMClient / WorkerAdapter (coding or independent review)
    -> shared native Claude Code / Codex command and trace capture
      -> Harbor DockerEnvironment start / stop
        -> private worker container
```

Harbor owns the worker container's Compose lifecycle. Evolve retains its
provider gateway, evaluation broker, timeouts, role permissions and cleanup
authority. It uses the same container specification for direct Docker and
Harbor. The realized Harbor container is inspected before the CLI starts:
exact mounts and internal network, non-root user, read-only root, dropped
capabilities, private process/IPC namespaces and resource limits.

An independent reviewer is still a separate invocation with its own inputs and
no evaluation tools. Sharing the runtime does not merge reviewer and author
authority. No Harbor Trial scheduler, installed-agent setup, reward calculation
or ATIF conversion is introduced by this adapter.

## Evidence and cancellation

Native stdout and stderr are retained byte-for-byte by the existing capture
layer. Harbor setup and cleanup diagnostics live separately under
`trace/harbor/`, alongside the container inspection receipt. These files are
private controller evidence and are not mounted into the worker.

On timeout or cancellation, Evolve stops the container as well as the launcher
process. Cleanup is checked in `docker_cleanup.json`. A nonzero CLI exit does
not erase an output solver; existing artifact validation and independent
admission checks still decide whether it may be used.

```bash
OPE_RUN_HARBOR_TESTS=1 OPE_GATEWAY_IMAGE=optiprofiler-evolve-gateway:latest \
  python -m unittest discover -s tests -p test_harbor_worker.py -v
```

These tests use a synthetic CLI inside real Harbor/Docker containers. They
consume no model quota and cover raw bytes, nonzero exit, cancellation, timeout,
mount protection, egress denial and resource cleanup. They do not substitute
for a live-provider smoke or numerical evaluator validation.

## Limits

Run the controller on the Docker host (or a local Docker VM with shared run
directories). A remote `DOCKER_HOST` does not transfer local bind sources.
Use a clean checkout on the remote machine instead.

Worker isolation is not the same as candidate evaluation isolation. Harbor
does not, by itself, separate arbitrary solver code from an evaluator's Python
interpreter. The evaluator boundary must be verified independently; do not
interpret this backend as certification against benchmark tampering.

This first adapter adds an optional backend rather than claiming a reduction
in total code size. The common container specification avoids duplicating
security settings, and the common `LLMClient` already serves all research roles.
