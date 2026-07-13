# Contributing

This repository is intentionally one package. Changes here must not edit,
vendor, or write into sibling `optiprofiler`, platform, agent, or problem-library
repositories.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the lightweight checks before spending model tokens:

```bash
ruff check src tests scripts examples
python scripts/check_markdown_links.py
python -m unittest discover -s tests -p 'test_*.py' -v
python -m build
python scripts/check_wheel.py "$(find dist -name '*.whl' -print -quit)"
```

These tests use fake workers and evaluators for controller behavior. A real
example invokes a model provider and OptiProfiler and should be run separately.

## Where to make a change

| Change | Primary code | Required companion work |
|---|---|---|
| Public function behavior | `src/optiprofiler_evolve/api.py` | API reference and public-surface tests |
| Add or change a config field | `src/optiprofiler_evolve/config.py` | JSON Schema, config reference, validation tests |
| Problem selection/splitting | `src/optiprofiler_evolve/data.py` | deterministic manifest tests |
| Evaluator/runtime adapter | `src/optiprofiler_evolve/evaluation.py` | runner, result-contract tests, security notes |
| Codex/Claude command | `src/optiprofiler_evolve/harness.py` | command tests and worker-image check |
| Worker mounts/resources | `src/optiprofiler_evolve/sandbox.py` | isolation tests and security notes |
| Worker evaluation tools | `src/optiprofiler_evolve/broker.py` | quota and hidden-access tests |
| Worker task/context | `src/optiprofiler_evolve/prompt.py` | prompt tests and experiment note |
| Population scheduling | `src/optiprofiler_evolve/engine.py` | deterministic lineage/checkpoint tests |
| Solver copying/interface rules | `src/optiprofiler_evolve/solver.py` | single-file and repository tests |

The complete flow and ownership boundaries are documented in
[Architecture](docs/architecture.md). Keep the top-level public surface limited
to `evolve` and `__version__`; new capabilities should normally be config fields
or internal adapters.

## Change discipline

1. Preserve the immutable initial solver and exact data manifest.
2. Keep hidden problem names and final evaluation controller-only.
3. Never let worker-controlled paths select host output locations.
4. Keep candidate/reference comparison inside one canonical benchmark call.
5. Record enough resolved state to explain and reproduce a run.
6. Prefer a small adapter over runtime-specific branches throughout the engine.
7. Update the relevant reference and example in the same commit as behavior.

Do not run a large evolution to validate a structural change. Unit tests, a
fake-worker engine test, and at most one one-round real smoke are the expected
progression.
