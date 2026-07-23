# Model providers and agent workers

OptiProfiler Evolve runs Codex CLI or Claude Code as a real coding agent. The
CLI owns the model conversation and tool loop. The controller owns the private
workspace, provider route, evaluation capabilities, timeout, raw trace, and
acceptance gates.

In the Docker backend, every agent invocation uses a short-lived provider
gateway. This applies to explorer workers, integrity reviewers, and research
roles. The gateway is model transport, not a general HTTP proxy.

The reviewed 4A transport checkpoint deliberately refuses to launch a
gateway-configured Docker worker until the 4B sidecar/network lifecycle is
attached. It never falls back to the older direct-credential path during that
intermediate state.

## Credential and network boundary

For a gateway-routed worker:

1. the controller resolves exactly one provider credential;
2. the real value is delivered only to the gateway sidecar;
3. the worker receives a non-secret dummy credential and an internal gateway URL;
4. the gateway replaces authentication and forwards only to the pinned origin;
5. the gateway forwards request and response bodies without persisting them;
   the controller separately preserves the effective prompt and every byte the
   CLI emits on stdout/stderr in the private agent trace.

Unrelated secret-named values and unrelated `pass_env` entries fail before
launch. Codex provider profiles and worker-authored provider arguments also fail
when gateway routing is enabled. This prevents a config from silently replacing
the controller-owned route.

General worker egress is a separate capability. `workers.tools.network` controls
shell/package-manager networking; provider transport continues through the
gateway when that setting is false. Direct provider access from a Docker worker
requires both `workers.allow_direct_provider=true` and
`sandbox.allow_direct_network=true`. These flags are unsafe experiment
provenance, not recommended defaults. `unsafe_local` stays direct and does not
claim gateway isolation.

## Compatibility matrix

| Worker harness | Gateway protocol | Allowed provider paths | Requirement |
|---|---|---|---|
| Claude Code | Anthropic Messages | `POST /v1/messages`, `POST /v1/messages/count_tokens`, `GET /v1/models` | Anthropic API or a compatible Messages/tool endpoint |
| Codex CLI | OpenAI Responses | `POST /v1/responses`, `GET /v1/models` | Responses streaming and function-tool compatibility |

The gateway rejects every other path, `CONNECT`, absolute-form request targets,
encoded paths, transfer-encoded requests, duplicate routing headers, and bodies
over the configured request limit. It ignores the worker's `Host`, strips
forwarding/hop-by-hop/authentication headers, does not follow redirects, and
rejects upstream DNS results that are not public. Responses, including SSE, are
forwarded incrementally rather than buffered to the request limit.

## Claude Code

For Anthropic directly:

```yaml
workers:
  pool:
    - harness: claude
      model: ${OPTIPROFILER_EVOLVE_MODEL}
      pass_env: [ANTHROPIC_API_KEY]
      provider_gateway:
        upstream_base_url: https://api.anthropic.com
        credential_env: ANTHROPIC_API_KEY
        auth_mode: x-api-key
```

For an Anthropic-compatible provider:

```yaml
workers:
  pool:
    - harness: claude
      model: ${OPTIPROFILER_EVOLVE_MODEL}
      env:
        ANTHROPIC_AUTH_TOKEN: ${OPTIPROFILER_EVOLVE_API_KEY}
      provider_gateway:
        upstream_base_url: ${OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL}
        credential_env: ANTHROPIC_AUTH_TOKEN
        auth_mode: bearer
```

The controller sets `ANTHROPIC_BASE_URL` to the internal gateway and supplies a
dummy `ANTHROPIC_API_KEY`; neither the true key nor the external base URL is
worker-visible.

## Codex

For OpenAI directly:

```yaml
workers:
  pool:
    - harness: codex
      model: ${OPTIPROFILER_EVOLVE_CODEX_MODEL}
      pass_env: [OPENAI_API_KEY]
      provider_gateway:
        upstream_base_url: https://api.openai.com/v1
        credential_env: OPENAI_API_KEY
        auth_mode: bearer
```

For another Responses-compatible provider, replace the upstream base URL and
credential name. Do not add `model_provider` arguments. The controller launches
Codex with ignored user config and a generated provider definition whose base
URL is the internal gateway's `/v1` endpoint. If that mapping cannot be
constructed, launch fails; it never falls back to the built-in provider.

## Preflight

The regular static check validates the worker config, credential names, CLI,
and agent-mode flags without a model call:

```bash
python scripts/check_worker_setup.py examples/experiment.yaml
```

The Codex route preflight uses the installed CLI but no provider token. It runs
Codex against a local fake Responses server and succeeds only if the fake
upstream observes exactly `POST /v1/responses` through the gateway:

```bash
PYTHONPATH=src python scripts/check_codex_gateway_route.py
```

The normal `--live` worker preflight consumes provider quota. It launches the
configured worker in its Docker boundary and requires the model to use a tool to
create and verify a probe file:

```bash
python scripts/check_worker_setup.py examples/experiment.yaml --live
```

Run the live check once for every distinct harness/provider/model entry before
a long experiment.

## Web search

`workers.tools.web_search=true` requests the selected harness's native search
tool. Claude receives `WebSearch` and `WebFetch`; Codex receives `--search`.
Those tools work only when the pinned provider supports them. Worker shell
networking is governed independently by `workers.tools.network` and the Docker
egress policy. A no-search ablation disables the native search option and
general egress; it does not disable model transport through the gateway.

## Evidence and reproducibility

- Pin the worker image, CLI version, model identifier, protocol, and upstream
  base URL for a reported experiment.
- Gateway audit records contain request IDs, protocol/path, status, timing, and
  byte counts only. They exclude bodies, headers, credentials, prompts, and
  solver source.
- Each record is appended and synchronized before the next request is counted.
  A failed audit write marks the gateway unhealthy, stops the sidecar, and
  makes its process exit nonzero. An upstream stream that ends early is recorded
  as `stream_interrupted`; the gateway does not append a second HTTP response.
- Worker stdout/stderr bytes remain authoritative in the private agent trace.
- Public run projections contain only gateway lifecycle status and aggregate
  request counts.

## Upstream references

- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [Claude Code LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [OpenAI Responses streaming events](https://platform.openai.com/docs/api-reference/responses-streaming)
