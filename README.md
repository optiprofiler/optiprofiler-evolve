# OptiProfiler Evolve

`optiprofiler-evolve` evolves a derivative-free optimization solver for one
fixed OptiProfiler problem-library experiment.

The user-facing API is one function:

```python
from optiprofiler_evolve import evolve

result = evolve(
    initial="./my_solver",
    interface="wrapper.py:solver",
    editable=["."],
    config="experiment.yaml",
    run_dir="runs/my_solver",
)
```

The controller copies `initial` and never edits it. Coding workers receive
independent solver workspaces, the public experiment manifest, and two tools:
`smoke_test` for a small public subset and `evaluate` for all public problems.
Hidden problems are evaluated only after the island finalists have been fixed.

## Alpha status

The Python path is an end-to-end MVP: repository candidates, deterministic data
splits, OptiProfiler scoring, Codex/Claude workers, Docker isolation, four-island
population evolution, checkpoints, migration, and final public+hidden reranking.

MATLAB entrypoints are detected from `.m` files but the MATLAB evaluator is not
implemented yet. Deliberately adversarial candidate code is also outside the
current threat model; see [Security](docs/security.md).

## Quick start

Install the package and build the two local images:

```bash
python -m pip install -e '.[dev]'
docker build -f docker/worker/Dockerfile -t optiprofiler-evolve-worker:latest .
docker build -f docker/evaluator/Dockerfile -t optiprofiler-evolve-evaluator:latest .
```

Set the model and provider credential used by the example, then run it:

```bash
export OPTIPROFILER_EVOLVE_MODEL='<model-id>'
export ANTHROPIC_API_KEY='<provider-key>'
python examples/run.py
```

Provider base URLs and CLI-specific options belong in the worker entry of the
YAML config. The engine does not hard-code model providers.

The quick start is intentionally small and is not a performance experiment.
Start with [Getting started](docs/getting-started.md), then copy the closest
example from [the examples index](examples/README.md).

## Fitness and data

Candidate and immutable initial solver are always passed together to the same
`optiprofiler.benchmark` call. Fitness is

```text
(candidate OptiProfiler score - initial OptiProfiler score + 1) / 2
```

so `0.5` is a tie, values above `0.5` improve on the initial solver, and values
below `0.5` regress. The run manifest freezes exact smoke, public, and hidden
problem names before any worker starts.

## Run artifacts

Each run keeps the resolved redacted config, exact data manifest, immutable
reference, candidate snapshots and lineage, worker transcripts, public
evaluation output, generation checkpoints, fixed finalists, final reranking,
and a materialized `final_solver/` directory.

## Documentation map

- [Getting started](docs/getting-started.md): install, run, and adapt a solver.
- [Public API reference](docs/api-reference.md): every `evolve(...)` argument and
  return field.
- [Configuration guide](docs/configuration.md): how the major experiment choices
  fit together.
- [Configuration reference](docs/config-reference.md): every YAML field, type,
  default, allowed value, and constraint.
- [JSON Schema](config.schema.json): editor completion and structural validation.
- [Examples](examples/README.md): Claude, Codex, and multi-file repository inputs.
- [Architecture](docs/architecture.md): execution flow and module boundaries.
- [Security](docs/security.md): enforced boundaries and current threat model.
- [MATLAB evaluator design](docs/matlab-evaluator-design.md): planned host-MATLAB
  adapter and candidate/reference path isolation.
- [Contributing](CONTRIBUTING.md): where to change the package and which checks to
  run.
- [Development plan](DEVELOPMENT_PLAN.md): current implementation milestones.
