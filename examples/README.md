# Examples

All examples use the public `evolve(...)` function. They intentionally run one
small iteration and demonstrate the interface rather than solver quality.
For convenience they explicitly reuse the mutation model as the mandatory
integrity reviewer. Paper-grade runs should configure a distinct
`integrity_review.worker`; every candidate then consumes one mutation call and
at least one review call.

| Goal | Python file | Config | Required environment |
|---|---|---|---|
| Claude Code quick start | `run.py` | `experiment.yaml` | `OPTIPROFILER_EVOLVE_MODEL`, `ANTHROPIC_API_KEY` |
| Claude-compatible provider | `run_claude_compatible.py` | `experiment-claude-compatible.yaml` | `OPTIPROFILER_EVOLVE_MODEL`, `OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL`, `OPTIPROFILER_EVOLVE_API_KEY` |
| Codex quick start | `run_codex.py` | `experiment-codex.yaml` | `OPTIPROFILER_EVOLVE_CODEX_MODEL`, `OPENAI_API_KEY` |
| Codex Responses-compatible provider | `run_codex_compatible.py` | `experiment-codex-compatible.yaml` | `OPTIPROFILER_EVOLVE_CODEX_MODEL`, `OPTIPROFILER_EVOLVE_OPENAI_BASE_URL`, `OPTIPROFILER_EVOLVE_API_KEY` |
| Multi-file solver repository | `run_repository.py` | `experiment.yaml` | Same as Claude quick start |
| Full research harness | `run.py` | `experiment-research.yaml` | Same as Claude quick start; edit `run.py` to point at this config |
| Single-job GitHub Actions launch | `github-actions/run.py` | `experiment.yaml` | Repository variable and Actions provider secret; see `github-actions/evolve.yml` |
| GitHub Actions from an external solver repository | `external-repository/evolve/run.py` | `external-repository/evolve/experiment.yaml` | Copy the shown repository layout and workflow |

Build the worker and evaluator images once before running an example. See
[Getting started](../docs/getting-started.md) for the commands.

The multi-file example deliberately uses a relative import. Candidate and
immutable-reference repositories are loaded under separate module namespaces,
so solver-internal Python imports should be relative whenever possible.

The compatible-provider example maps package-prefixed host variables to the
environment names expected by Claude Code. Keep credentials in `pass_env` or
`${ENV_NAME}` references; do not write credential values into YAML.

Run `python scripts/check_worker_setup.py <config>` from the repository root for
a no-token static check. Add `--live` to prove that the configured model can use
agent tools inside the worker sandbox before starting an evolution run. Provider
protocol requirements are documented in
[Model providers and agent workers](../docs/providers.md).

`experiment-research.yaml` enables all optional phases. Its scout and analyst
roles use isolated workspaces without `smoke_test` or `evaluate`; only the
controller can materialize their proposed variants and run validation. Start
with one iteration and two islands before using the checked-in research budgets.
