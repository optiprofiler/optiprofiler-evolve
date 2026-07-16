# Development Plan

## P0: End-to-end Python MVP

Implemented in `0.1.0a0`:

- one public `evolve(...)` API;
- complete solver repository snapshots and editable scopes;
- exact problem-library manifests with smoke/public/validation/hidden splits;
- normalized candidate-versus-fixed-reference OptiProfiler fitness;
- Codex and Claude Code worker harnesses without provider hard-coding;
- Docker worker/evaluator boundaries and quota-limited public tools;
- bounded islands, migration, checkpoints, validation selection, and one hidden evaluation;
- collaborator quick starts, complete API/config references, a checked-in JSON
  Schema, and Claude/Codex/multi-file examples;
- tests for source isolation, split integrity, broker capabilities, population
  flow, package surface, documentation drift, and a real S2MPJ benchmark tie.

## P1: Stabilize for team experiments

1. Add resume/retry semantics and deterministic run fingerprints.
2. Add the host-MATLAB evaluator without changing the public API. The user owns
   MATLAB installation/licensing; the adapter owns process invocation, exact
   problem manifests, and candidate/reference path isolation.
3. Integrate OptiProfiler's planned agent-oriented output mode so workers receive
   compact profile, per-problem, history-plot, and failure evidence rather than
   only scalar fitness.
4. Add controlled network egress and a hardened candidate execution boundary.
5. Test a provider/model matrix for Codex and Claude-compatible APIs.
6. Run reproducible BDS and multi-file NEWUOA team examples with fixed configs.

## P2: Research system

1. Validate the frozen public/validation/hidden protocol across problem libraries.
2. Compare islands, memory, migration, feedback bundles, tools, worker harnesses,
   models, and budgets through ablations.
3. Add richer parent selection and archive policies only when experiments show a
   clear benefit.
4. Add additional problem-library and evaluator adapters behind the same data and
   evaluation boundaries.
5. Validate on a real application-backed DFO problem in addition to standard
   CUTEst/S2MPJ suites.

The package should remain understandable: no additional top-level API, no generic
task framework, and no new subsystem without a tested responsibility boundary.
