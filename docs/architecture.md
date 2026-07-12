# Architecture

## Main flow

```text
evolve(...)
  -> validate config and solver interface
  -> copy immutable reference and seed candidate
  -> resolve exact problem names and freeze smoke/public/hidden split
  -> evaluate seed against itself (fitness 0.5)
  -> repeat for each generation
       -> select one parent per island
       -> copy each parent to a private worker workspace
       -> run Codex or Claude Code inside the worker sandbox
       -> expose smoke_test and public evaluate through a bounded broker
       -> validate the complete edited solver tree
       -> run one canonical public evaluation
       -> record candidate and trim each island population
       -> migrate champions at the configured interval
  -> freeze finalist set
  -> controller evaluates each finalist on public + hidden
  -> materialize final_solver and reports
```

## Module boundaries

| Module | Responsibility |
|---|---|
| `api.py` | The single public `evolve(...)` function |
| `config.py` | Strict YAML/mapping schema, validation, secret redaction |
| `solver.py` | Repository copies, interface checks, edit scope, tree safety |
| `data.py` | Problem-library selection and immutable split manifest |
| `evaluation.py` | Python OptiProfiler adapter and evaluation Docker boundary |
| `broker.py` | Quota-limited smoke/public tools; no hidden capability |
| `harness.py` | Codex and Claude Code argv construction |
| `sandbox.py` | Worker Docker process, resources, mounts, environment |
| `prompt.py` | DFO solver task card and public feedback context |
| `engine.py` | Islands, scheduling, population, migration, checkpoints, rerank |
| `models.py` | Internal immutable result records |

No module copies or modifies OptiProfiler source. `optiprofiler` is a normal
runtime dependency and remains a separate package.

## Repository candidates

`initial` may be a single file or a directory. A directory is treated as the
complete solver implementation. The interface uses
`relative/path.py:function`; Python solver-internal imports should be relative
so candidate and reference modules can coexist during one benchmark call.

Workers can add, delete, or reorganize any file matched by `editable`. The
controller snapshots the resulting complete tree, not a model-generated diff.
