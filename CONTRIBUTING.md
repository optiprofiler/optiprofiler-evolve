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
| Add or change a config field | `src/optiprofiler_evolve/shared/config.py` | JSON Schema, config reference, validation tests |
| Problem selection/splitting | `src/optiprofiler_evolve/shared/data.py` | deterministic manifest tests |
| Evaluator/runtime adapter | `src/optiprofiler_evolve/evaluation/evaluation.py` | runner, result-contract tests, security notes |
| Codex/Claude command | `src/optiprofiler_evolve/agents/harness.py` | command tests and worker-image check |
| Worker mounts/resources | `src/optiprofiler_evolve/agents/sandbox.py` | isolation tests and security notes |
| Worker evaluation tools | `src/optiprofiler_evolve/evaluation/broker.py` | quota and hidden-access tests |
| Worker task/context | `src/optiprofiler_evolve/phases/explore/prompt.py` | prompt tests and experiment note |
| Population scheduling | `src/optiprofiler_evolve/controller/engine.py` | deterministic lineage/checkpoint tests |
| Solver copying/interface rules | `src/optiprofiler_evolve/shared/solver.py` | single-file and repository tests |
| Phase implementation/context | `src/optiprofiler_evolve/phases/`, `controller/phase_contexts.py` | stage contracts, staged-state and deterministic workflow tests |
| Unified model invocation | `src/optiprofiler_evolve/agents/llm.py` | backend lifecycle, authority, output and trace tests |

The complete flow and ownership boundaries are documented in
[Architecture](docs/architecture.md). Keep the top-level public surface limited
to `evolve` and `__version__`; new capabilities should normally be config fields
or internal adapters.

Every phase has its own class and directory. Keep model invocation in
`LLMClient`, stage-specific prompts with their stage, and shared structures in
named shared modules. Move complete modules with `mv` when reorganizing; update
all imports and subprocess/Docker entrypoints rather than leaving old internal
module forwarding shims. The documented `serve` and `docker_gc` commands remain
supported entrypoints.

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
