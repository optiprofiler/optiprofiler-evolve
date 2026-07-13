# Examples

All examples use the public `evolve(...)` function. They intentionally run one
small generation and demonstrate the interface rather than solver quality.

| Goal | Python file | Config | Required environment |
|---|---|---|---|
| Claude Code quick start | `run.py` | `experiment.yaml` | `OPTIPROFILER_EVOLVE_MODEL`, `ANTHROPIC_API_KEY` |
| Codex quick start | `run_codex.py` | `experiment-codex.yaml` | `OPTIPROFILER_EVOLVE_CODEX_MODEL`, `OPENAI_API_KEY` |
| Multi-file solver repository | `run_repository.py` | `experiment.yaml` | Same as Claude quick start |

Build the worker and evaluator images once before running an example. See
[Getting started](../docs/getting-started.md) for the commands.

The multi-file example deliberately uses a relative import. Candidate and
immutable-reference repositories are loaded under separate module namespaces,
so solver-internal Python imports should be relative whenever possible.

For an OpenAI- or Anthropic-compatible third-party provider, add the base URL
and any provider-specific environment variables to the corresponding
`workers.pool` entry. Keep credentials in `pass_env` or `${ENV_NAME}` references;
do not write credential values into YAML.
