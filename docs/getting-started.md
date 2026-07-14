# Getting Started

This path takes a collaborator from a clean checkout to one small Python solver
evolution run. The example consumes model API tokens but is intentionally too
small to support a performance claim.

## 1. Prepare the repository

Requirements:

- Python 3.11 or newer;
- Docker with permission to run local containers;
- access to either Claude Code or Codex through a supported model provider.

Create an environment and install the package in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Build the isolated coding-worker and evaluator images:

```bash
docker build -f docker/worker/Dockerfile -t optiprofiler-evolve-worker:latest .
docker build -f docker/evaluator/Dockerfile -t optiprofiler-evolve-evaluator:latest .
```

Confirm that both images are usable before spending model tokens:

```bash
docker run --rm --entrypoint sh optiprofiler-evolve-worker:latest -lc \
  'python --version && codex --version && claude --version'
docker run --rm --entrypoint python optiprofiler-evolve-evaluator:latest -c \
  'import optiprofiler, optiprofiler_evolve'
```

Keep the repository, solver input, and `run_dir` under a host directory shared
with Docker. For example, Colima commonly shares `/Users` but not
`/private/tmp`.

## 2. Run one small experiment

For Claude Code:

```bash
export OPTIPROFILER_EVOLVE_MODEL='<model-id>'
export ANTHROPIC_API_KEY='<provider-key>'
python examples/run.py
```

For Codex:

```bash
export OPTIPROFILER_EVOLVE_CODEX_MODEL='<model-id>'
export OPENAI_API_KEY='<provider-key>'
python examples/run_codex.py
```

For Claude Code with an Anthropic-compatible endpoint, use the dedicated
example so provider-specific variables stay outside the YAML file:

```bash
export OPTIPROFILER_EVOLVE_MODEL='deepseek-v4-flash'
export OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL='https://api.deepseek.com/anthropic'
export OPTIPROFILER_EVOLVE_API_KEY='<provider-key>'
python examples/run_claude_compatible.py
```

The model identifier and endpoint are provider-specific. The engine passes
them unchanged to Claude Code. Credential values are expanded in memory and
redacted from `resolved_config.json`.

The script prints the run directory, final solver directory, public score, and
controller-only final score. A timestamped run directory is created under
`runs/` unless `run_dir` is supplied explicitly.

## 3. Adapt a solver

Point `initial` at either one Python file or a repository directory. The
recommended repository form is:

```text
my_solver/
  solver.py       # declares solver(...)
  search.py       # solver-owned helper
  tests/          # optional worker-written checks
```

Then declare the entrypoint and editable surface explicitly:

```python
from optiprofiler_evolve import evolve

result = evolve(
    initial="./my_solver",
    interface="solver.py:solver",
    editable=["*.py", "tests/**"],
    config="experiment.yaml",
)
```

Use relative imports such as `from .search import step` inside a multi-file
Python solver. The evaluator loads candidate and immutable reference under
separate module namespaces during the same OptiProfiler benchmark call.

## 4. Scale deliberately

Before increasing model spend:

1. Confirm the seed evaluates successfully against itself.
2. Keep `rounds: 1`, `islands: 1`, and a small explicit problem list.
3. Inspect `public_data_manifest.json`, `resolved_config.json`, worker
   transcripts, and evaluation feedback in the run directory.
4. Increase problem count and evaluation budget.
5. Increase islands, rounds, and worker parallelism last.

The hidden split is not exposed through worker tools. It is used only after the
finalist set has been fixed. Do not use final reranking repeatedly as worker
feedback.

## 5. Understand search and isolation

Web search requires both `workers.tools.web_search: true` and
`workers.tools.network: true`. Claude workers receive `WebSearch` and
`WebFetch`; Codex workers are launched with `--search`. Compatible model
endpoints do not always expose every built-in harness tool, so the research
worker image also provides `ddgr` as a provider-independent shell fallback.
The selected CLI and provider must still support tool calling. To disable all
outbound access, set both fields to `false`; disabling only the search tool does
not disable the container network or remove shell networking tools.

With the default Docker backend, every attempt gets a separate writable copy of
its parent solver, container, broker token, and temporary Docker network. A
worker can see that copy, read-only evaluation artifacts, and its private
`smoke_test`/`evaluate` broker. It cannot see the original solver, immutable
reference, hidden manifest, host home, sibling repositories, other worker
networks, or Docker socket. The container root is read-only and Linux
capabilities are dropped. `sandbox.backend: unsafe_local` removes these
boundaries and should be used only for trusted package tests.

## 6. Find the right reference

- Function arguments and return values: [Public API](api-reference.md)
- Every YAML field: [Configuration reference](config-reference.md)
- How options work together: [Configuration guide](configuration.md)
- Package internals: [Architecture](architecture.md)
- Modifying the package: [Contributing](../CONTRIBUTING.md)
- Current isolation guarantees: [Security](security.md)
