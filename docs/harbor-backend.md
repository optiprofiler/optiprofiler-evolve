# Optional Harbor backend

Harbor is an environment backend, not a second evolution engine. The default
remains `workers.adapter: cli`. To use Harbor, install on Python 3.12 or newer:

```bash
python -m pip install 'optiprofiler-evolve[harbor]'
docker compose version
docker build -f docker/solver/Dockerfile -t optiprofiler-evolve-solver:latest .
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
Run the Harbor controller as a non-root user: its UID is mapped into the worker
so it can write its own run directory without receiving root authority.

## Codex subscription login

The complete [subscription example](../examples/experiment-codex-subscription.yaml)
runs a two-island, one-iteration smoke with a required solver explanation:
`python examples/run_codex_subscription.py`. Build the worker, scorer, and numeric
solver images using [Getting started](getting-started.md) first. Its small budget
tests the integration, not solver quality; use `experiment-research.yaml` when
you also want the optional scout, ablation, and strategy-transfer stages.

Log in once on the Docker host with `codex login`. Evolve can consume the same
`CODEX_AUTH_JSON_PATH` convention as Harbor's Codex installed agent:

```yaml
workers:
  adapter: harbor
  allow_direct_provider: true
  pool:
    - harness: codex
      model: gpt-5.3-codex-spark
      env:
        CODEX_AUTH_JSON_PATH: ${HOME}/.codex/auth.json
sandbox:
  backend: docker
  allow_direct_network: true
```

This is an explicit owner-trusted subscription mode, not the default gateway
boundary. The login file is mounted read-only and copied into the invocation's
temporary Codex home, which disappears with the container. The worker can read
that login and has direct service access. Do not use this mode for untrusted
third-party tasks. Do not commit credentials, use `CODEX_FORCE_AUTH_JSON=1`, or
mix this path with API-key/provider routing. Missing login files fail at launch.
Each role still gets a separate container and no evaluator implementation.
The adapter needs outbound access for Codex even when a role has no web-search
tools. Disabling web search is not a network firewall in this direct mode.
For a second allowed model, change `model` explicitly to `gpt-5.5`; Evolve does
not automatically change providers/models on quota errors.

In standalone Harbor, the corresponding option is
`--ae CODEX_AUTH_JSON_PATH=$HOME/.codex/auth.json`. Evolve runs its own native CLI
adapter over Harbor environments; it does not call `harbor run` internally.

## Claude subscription login

A Claude Code worker can use an owner login file the same way. Name the file
explicitly; Evolve never searches a home directory for credentials:

```yaml
workers:
  adapter: harbor
  allow_direct_provider: true
  pool:
    - harness: claude
      model: <model id>
      env:
        CLAUDE_CREDENTIALS_JSON_PATH: ${HOME}/.config/ope-claude/credentials.json
        # Claude Code starts helper calls on its own small/fast model unless told otherwise.
        ANTHROPIC_SMALL_FAST_MODEL: <model id>
        ANTHROPIC_DEFAULT_HAIKU_MODEL: <model id>
        CLAUDE_CODE_SUBAGENT_MODEL: <model id>
sandbox:
  backend: docker
  allow_direct_network: true
```

The same trust statement as the Codex mode applies. Only the *path* enters the
worker environment, so `launch.json`, the Compose file and `container_inspect.json`
hold no credential. The file is mounted read-only and copied to the invocation's
temporary `CLAUDE_CONFIG_DIR`. Do not pass a token through `env`: environment
values are persisted in that launch evidence. The path cannot be combined with
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`CLAUDE_CODE_OAUTH_TOKEN` or a Codex login.

Claude Code's `--bare` mode never reads an OAuth login, so only this worker runs
with `--setting-sources user --strict-mcp-config` instead. Its user settings
directory is an empty temporary home, and project settings are excluded because a
candidate tree can carry a `.claude/` directory into a later worker's workspace.
API-key and gateway workers keep `--bare` unchanged.

A copied login is valid only while its access token is. A refresh performed inside
a worker is discarded with the container and may invalidate the login it was copied
from, so check the expiry against the whole run before launching and copy a fresh
file per run. Each trace `outcome.json` records `model_evidence`: the models the
native stream reported (`result_model_usage`, which includes helper calls, or
`assistant_messages` when a stream ended early). Treat the requested `model` as
configuration and `model_evidence` as the observation; enforcing a model policy is
left to the experiment, not to the package.

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
exact mounts and declared network policy, non-root user, read-only root, dropped
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
Repeated preparation uses a new `harbor-retry-*` directory rather than overwriting
the first invocation's launch or inspection evidence.

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

The Docker evaluator now uses a separate numeric-only solver image and a
bounded JSON oracle protocol. See [numeric isolation](numeric-isolation.md).
Both the scorer and solver image must be rebuilt when upgrading this boundary.

This first adapter adds an optional backend rather than claiming a reduction
in total code size. The common container specification avoids duplicating
security settings, and the common `LLMClient` already serves all research roles.
