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

## 5. Find the right reference

- Function arguments and return values: [Public API](api-reference.md)
- Every YAML field: [Configuration reference](config-reference.md)
- How options work together: [Configuration guide](configuration.md)
- Package internals: [Architecture](architecture.md)
- Modifying the package: [Contributing](../CONTRIBUTING.md)
- Current isolation guarantees: [Security](security.md)
