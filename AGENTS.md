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

- Controller commits are the sole writer of run state, candidate acceptance,
  and checkpoints. The coordinator orders phases; application binds infrastructure
  and the authoritative event sink.
- Workers edit private candidate copies. They cannot see validation or hidden
  data, the immutable reference, OptiProfiler source, sibling repositories, the
  host home, or the Docker socket.
- Public evaluation may guide workers. Validation is controller-only. Hidden is
  evaluated once after the champion is fixed and never feeds back.
- The default workflow remains `prepare -> explore -> validate -> hidden ->
  report`. Custom ordered lists, including empty lists, use declared artifact
  contracts and preflight validation; do not add a generic DAG compiler.
- Selecting a champion closes candidate admission and reselection. Hidden is
  single-use even on failure; a required lockbox cannot be omitted when returning
  a champion. Application constructs `EvolveResult`, independently of report.
- A worker harness is a real Codex or Claude Code agent process, not a raw text
  completion. Provider support depends on the CLI's API dialect and tool loop.
- Keep credentials in environment variables. Never write them into YAML, CLI
  arguments, traces, fixtures, or committed files.

## Where changes belong

| Concern | Module or document |
|---|---|
| Public call | `src/optiprofiler_evolve/api.py` |
| Typed config | `config.py`, `config.schema.json`, `docs/config-reference.md` |
| Workflow contracts | `phases/contracts.py`, `controller/contracts.py`, `controller/registry.py` |
| Run composition and infrastructure lifecycle | `application/run.py` |
| Population and selection | `controller/state.py`, `controller/commits.py`, `controller/coordinator.py`, `controller/selection.py` |
| Phase input binding | `controller/phase_contexts.py`, `controller/runtime.py` |
| Attempts and policies | `phases/attempts/runner.py`, `phases/attempts/steps.py`, `phases/attempts/policies.py` |
| Unified LLM entry and isolation | `agents/llm.py`, `agents/workers.py`, `agents/harness.py`, `agents/sandbox.py` |
| OptiProfiler calls and public broker | `criteria/performance/evaluator.py`, `agents/broker.py` |
| Solver/data boundaries | `solver.py`, `data.py` |
| Research evidence | `phases/_research.py`, `criteria/performance/evidence.py` |
| Criteria and scoring | `criteria/contracts.py`, `criteria/runner.py`, `criteria/{performance,complexity,integrity}/` |
| Events, reports and local views | `observability/`, `observability/views/` |

Each phase has one module, `phases/<name>.py`, containing its class and specific
context/result types. Keep subsystem-only helpers inside their owning package;
only genuinely cross-subsystem foundations belong at the package root. Do not
reintroduce generic `shared` or `quality` buckets or single-file phase packages.

Criteria own both `assess` and `generation_guidance`. Use `SolverJob` for solver
proposal/edit tasks; the unified LLM entrypoint supplies their criterion guidance.
Keep review jobs independent, and expose only explicitly public rule fields to
generation guidance, never evaluation data, private results or credentials.

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
