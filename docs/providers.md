# Model Providers and Agent Workers

OptiProfiler Evolve does not call a text-completion API directly. Each worker is
a real coding-agent process: either Codex CLI or Claude Code. The selected CLI
owns the model conversation and tool loop; OptiProfiler Evolve owns the private
workspace, evaluation capabilities, timeout, transcript, and acceptance gates.

This distinction matters for compatibility. A provider is usable only when it
implements the API dialect and tool-calling behavior required by the selected
CLI. A model name and a base URL alone are not a compatibility guarantee.

## Compatibility matrix

| Worker harness | Provider requirement | Agent tools | Recommended use |
|---|---|---|---|
| Claude Code | Anthropic API or a sufficiently compatible Messages endpoint | `Read`, `Edit`, `Write`, optional `Bash`, `WebSearch`, and `WebFetch` | Broadest route for Anthropic-compatible gateways and coding plans |
| Codex CLI with OpenAI | OpenAI authentication and a Codex-capable model | Local shell/edit loop; optional native Responses web search | OpenAI models |
| Codex CLI with a custom provider | Responses API compatibility, including function tools and streaming | Local shell/edit loop; native web search only if the provider supports it | OpenAI Responses-compatible gateways |
| Chat-Completions-only endpoint | Not sufficient for the built-in Codex harness | N/A | Put a compatible gateway in front of it or use a Claude-compatible endpoint |

Codex officially exposes custom providers through `model_provider` and
`model_providers.<id>` configuration. Its custom-provider wire protocol is the
Responses API. Claude Code officially supports gateway routing through
`ANTHROPIC_BASE_URL` and token environment variables. Provider implementations
still differ in accepted content blocks, tool schemas, caching, and server-side
search, so run the live probe below before a multi-iteration experiment.

## Claude Code

For Anthropic directly, copy `examples/experiment.yaml` and export:

```bash
export OPTIPROFILER_EVOLVE_MODEL='<provider-model-id>'
export ANTHROPIC_API_KEY='<provider-key>'
```

For an Anthropic-compatible endpoint, copy
`examples/experiment-claude-compatible.yaml`:

```yaml
workers:
  pool:
    - harness: claude
      model: ${OPTIPROFILER_EVOLVE_MODEL}
      env:
        ANTHROPIC_BASE_URL: ${OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL}
        ANTHROPIC_AUTH_TOKEN: ${OPTIPROFILER_EVOLVE_API_KEY}
```

```bash
export OPTIPROFILER_EVOLVE_MODEL='<provider-model-id>'
export OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL='<anthropic-compatible-base-url>'
export OPTIPROFILER_EVOLVE_API_KEY='<provider-key>'
python examples/run_claude_compatible.py
```

The harness starts Claude Code in non-interactive agent mode with a bounded tool
list and stream-JSON tracing. `--print` means "run the agent and return when it
finishes"; it does not turn Claude Code into a one-shot text completion.

## Codex

For OpenAI directly, copy `examples/experiment-codex.yaml` and export:

```bash
export OPTIPROFILER_EVOLVE_CODEX_MODEL='<codex-model-id>'
export OPENAI_API_KEY='<openai-key>'
```

For a custom Responses-compatible endpoint, copy
`examples/experiment-codex-compatible.yaml`. It passes an explicit provider
definition to Codex instead of assuming that `OPENAI_BASE_URL` changes the
built-in OpenAI provider:

```yaml
workers:
  pool:
    - harness: codex
      model: ${OPTIPROFILER_EVOLVE_CODEX_MODEL}
      env:
        CODEX_PROVIDER_API_KEY: ${OPTIPROFILER_EVOLVE_API_KEY}
      args:
        - --config
        - 'model_provider="compatible"'
        - --config
        - 'model_providers.compatible.name="Compatible provider"'
        - --config
        - 'model_providers.compatible.base_url="${OPTIPROFILER_EVOLVE_OPENAI_BASE_URL}"'
        - --config
        - 'model_providers.compatible.env_key="CODEX_PROVIDER_API_KEY"'
        - --config
        - 'model_providers.compatible.wire_api="responses"'
```

Environment references are expanded before the argv is built, including strings
inside `args`. Use inline references only for non-secret values such as a base
URL; arguments are preserved in provenance and are not a credential store:

```bash
export OPTIPROFILER_EVOLVE_CODEX_MODEL='<provider-model-id>'
export OPTIPROFILER_EVOLVE_OPENAI_BASE_URL='<responses-compatible-base-url>'
export OPTIPROFILER_EVOLVE_API_KEY='<provider-key>'
python examples/run_codex_compatible.py
```

## Prove agent mode before evolving

The static check validates the config, required credentials, selected CLI, and
agent-mode flags without making a model request:

```bash
python scripts/check_worker_setup.py examples/experiment-claude-compatible.yaml
```

The live check consumes a small amount of provider quota. It launches the exact
configured worker in its Docker boundary and succeeds only when the model uses a
tool to create and read back a probe file in its private workspace:

```bash
python scripts/check_worker_setup.py \
  examples/experiment-claude-compatible.yaml --live
```

Run the probe once for every distinct harness/provider/model entry used in an
experiment. Use `--worker-index` for a mixed worker pool. The probe transcript is
kept under `build/worker-preflight/`. The synthetic prompt contains no solver or
experiment data; still treat provider transcripts as potentially sensitive
diagnostic artifacts.

## Web search

`workers.tools.network: true` allows the remote model API and outbound shell
networking. `workers.tools.web_search: true` additionally requests the harness's
native search tool. Native search is provider-dependent:

- Claude Code receives `WebSearch` and `WebFetch` when enabled.
- Codex receives `--search`, which requires provider support for the Responses
  web-search tool.
- The worker image includes `ddgr` as a shell fallback. The worker prompt tells
  agents to use it when a compatible endpoint lacks native search.

The fallback proves outbound search, not native server-tool compatibility. For a
strict no-search ablation, disable both `web_search` and `network`; disabling only
`web_search` still leaves shell networking available.

## Credentials and reproducibility

- Never commit credential values. Use `pass_env` or `${ENV_NAME}` references.
- Resolved credentials are redacted from `resolved_config.json`.
- The run records the harness, model identifier, CLI version, image identity,
  and redacted provider configuration.
- Pin the worker image and model identifier for a reported experiment. A provider
  alias may change behavior without a package change.

Provider configuration is intentionally data, not engine code. Adding a new
compatible endpoint should require a new worker entry or example, not a change to
the population controller.

## Upstream references

- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [Claude Code LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)
