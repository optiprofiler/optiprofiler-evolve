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
independent solver workspaces, an anonymous experiment manifest, and two tools:
`smoke_test` for a small public subset and `evaluate` for all public problems.
Validation selects one champion inside the controller; hidden problems evaluate
that fixed champion once and never influence the worker or population loop.

## Alpha status

The Python path is an end-to-end MVP: repository candidates, deterministic data
splits, OptiProfiler scoring, Codex/Claude workers, Docker isolation, four-island
population evolution, checkpoints, migration, validation selection, and a final
hidden holdout evaluation. An optional full research workflow adds a direction
scout, per-island strategy attribution with executable leave-one-out ablations,
bounded cross-island recombination, and a post-selection strong-challenger
report. These are configured phases, not additional public APIs.

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

For an Anthropic-compatible endpoint, use the checked-in mapping example:

```bash
export OPTIPROFILER_EVOLVE_MODEL='<model-id>'
export OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL='<anthropic-compatible-url>'
export OPTIPROFILER_EVOLVE_API_KEY='<provider-key>'
python examples/run_claude_compatible.py
```

External problem libraries such as PyCUTEst use OptiProfiler's current
problem-library plugin protocol and must be installed separately in the
evaluator environment. Until the corresponding OptiProfiler release is on
PyPI, install the current OptiProfiler source before this package or build an
experiment image from clean source checkouts. Bundled legacy libraries remain
compatible with OptiProfiler 1.3.x.

Provider base URLs and CLI-specific options belong in the worker entry of the
YAML config. The engine does not hard-code model providers or credentials.
Read [Model providers and agent workers](docs/providers.md) before using a
third-party endpoint: Claude-compatible and Codex Responses-compatible APIs use
different configuration, and a base URL alone does not prove agent-tool support.

The quick start is intentionally small and is not a performance experiment.
Start with [Getting started](docs/getting-started.md), then copy the closest
example from [the examples index](examples/README.md). Use
`examples/experiment-research.yaml` only after the small exploration-only run
works; it is intentionally much more expensive.

## Fitness and data

Candidate and the configured fixed reference solver are always passed together to the same
`optiprofiler.benchmark` call. Fitness is

```text
(candidate OptiProfiler score - reference OptiProfiler score + 1) / 2
```

so `0.5` is a tie, values above `0.5` improve on the reference solver, and values
below `0.5` regress. The trusted run manifest freezes exact smoke, public,
validation, and hidden problem names before any worker starts. Worker-visible
artifacts use opaque problem identifiers.

## Run artifacts

Each run keeps the resolved redacted config, exact data manifest, immutable
reference, candidate snapshots and lineage, worker transcripts, public
evaluation output, iteration checkpoints, validation selection, hidden evaluation,
and a materialized `final_solver/` directory. Full research runs also preserve
direction cards, per-island source diffs and strategy cards, executable ablation
matrices, island bundles, recombination conflicts/results, and a challenger
report under `research/`.

## Documentation map

- [Agent guide](AGENTS.md): compact repository navigation, invariants, and
  verification commands for coding agents working on the package.
- [Getting started](docs/getting-started.md): install, run, and adapt a solver.
- [Public API reference](docs/api-reference.md): every `evolve(...)` argument and
  return field.
- [Configuration guide](docs/configuration.md): how the major experiment choices
  fit together.
- [Configuration reference](docs/config-reference.md): every YAML field, type,
  default, allowed value, and constraint.
- [Model providers and agent workers](docs/providers.md): Claude/Codex provider
  compatibility, credential mapping, agent-mode probes, and search behavior.
- [JSON Schema](config.schema.json): editor completion and structural validation.
- [Examples](examples/README.md): Claude, Codex, and multi-file repository inputs.
- [Architecture](docs/architecture.md): execution flow and module boundaries.
- [Research workflow](docs/research-workflow.md): optional scout, attribution,
  ablation, recombination, and challenger phases.
- [Architecture constitution](docs/architecture-constitution.md): stable
  ownership, extension, safety, and provenance rules.
- [Security](docs/security.md): enforced boundaries and current threat model.
- [MATLAB evaluator design](docs/matlab-evaluator-design.md): planned host-MATLAB
  adapter and candidate/reference path isolation.
- [Contributing](CONTRIBUTING.md): where to change the package and which checks to
  run.
- [Development plan](DEVELOPMENT_PLAN.md): current implementation milestones.
