# Agent Guide

This repository implements a small, benchmark-driven harness for evolving
derivative-free optimization solvers. The public package API is intentionally
limited to:

```python
from optiprofiler_evolve import evolve
```

Do not modify sibling repositories such as `optiprofiler`, the platform, the
agent, or problem libraries while working here. They are independent packages.

## Read before changing code

1. `README.md` for scope and the shortest user path.
2. `docs/getting-started.md` for a clean-checkout run.
3. `docs/configuration.md` and `docs/config-reference.md` for experiment config.
4. `docs/providers.md` for Codex/Claude agent and API compatibility.
5. `docs/architecture.md` for execution flow and module ownership.
6. `docs/research-workflow.md` for optional scout, strategy-ablation,
   recombination, and challenger phases.
7. `docs/architecture-constitution.md` before changing a protocol or ownership
   boundary.

## Stable invariants

- The engine is the sole writer of population state, candidate acceptance,
  checkpoints, and authoritative events.
- Workers edit private candidate copies. They cannot see validation or hidden
  data, the immutable reference, OptiProfiler source, sibling repositories, the
  host home, or the Docker socket.
- Public evaluation may guide workers. Validation is controller-only. Hidden is
  evaluated once after the champion is fixed and never feeds back.
- Core workflow order remains `prepare -> explore -> validate -> hidden ->
  report`. Optional research phases use the declared phase contracts; do not add
  a generic DAG compiler.
- A worker harness is a real Codex or Claude Code agent process, not a raw text
  completion. Provider support depends on the CLI's API dialect and tool loop.
- Keep credentials in environment variables. Never write them into YAML, CLI
  arguments, traces, fixtures, or committed files.

## Where changes belong

| Concern | Module or document |
|---|---|
| Public call | `src/optiprofiler_evolve/api.py` |
| Typed config | `config.py`, `config.schema.json`, `docs/config-reference.md` |
| Workflow contracts | `protocols.py`, `registry.py`, `phases/`, `steps/`, `policies/` |
| Population and selection | `engine.py` |
| CLI agents and isolation | `workers.py`, `harness.py`, `sandbox.py` |
| OptiProfiler calls and public broker | `evaluation.py`, `broker.py` |
| Solver/data boundaries | `solver.py`, `data.py` |
| Research evidence | `research.py`, `phases/research.py` |
| Events and local views | `events.py`, `provenance.py`, `viewers.py` |

When adding a config field, update the dataclass, JSON Schema, configuration
reference, a checked-in example when relevant, and tests. Documentation tests
fail when the schema/reference field sets drift.

## Verification

Use a headless Matplotlib backend on macOS:

```bash
MPLBACKEND=Agg python -m pytest -q
ruff check src tests scripts examples
python scripts/check_markdown_links.py
git diff --check
```

Before spending evolution budget, validate each distinct worker entry:

```bash
python scripts/check_worker_setup.py examples/experiment.yaml
python scripts/check_worker_setup.py examples/experiment.yaml --live
```

The live check consumes provider quota and must remain a synthetic tool-use
probe. Do not put source code or experiment data into that prompt.
