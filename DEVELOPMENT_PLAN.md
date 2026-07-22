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
3. Integrate OptiProfiler's planned agent-oriented output mode through a stable,
   versioned `agent_report` contract. The report should provide a compact index,
   named profile scores, ranked problem-level diagnostics, failure and budget
   signals, selected pre-rendered plots with structured sidecars, and links to
   the complete raw artifacts. Workers should find high-value evidence without
   scanning every PDF or inferring numeric facts from images alone.
4. Add controlled network egress and a hardened candidate execution boundary.
5. Test a provider/model matrix for Codex and Claude-compatible APIs.
6. Run reproducible BDS and multi-file NEWUOA team examples with fixed configs.

## P2: Research system

Implemented as an optional, versioned-artifact workflow in the current source:

- shared/per-island/off direction scouting with guided and unguided islands;
- per-island source-diff strategy hypotheses and executable removal ablations;
- controller validation selection of bounded island bundles;
- conflict-explicit, bounded portable-patch recombination;
- a post-hidden public challenger report that cannot change selection;
- role workspaces without evaluator capabilities and graceful scout/analyst fallback.

Remaining research work:

1. Validate the frozen public/validation/hidden protocol across problem libraries.
2. Compare scout modes, islands, memory, migration, feedback bundles, tools, worker harnesses,
   models, and budgets through ablations.
3. Replace the internal legacy evidence index with OptiProfiler's versioned
   `agent_report` once that package contract exists; add canonical problem contrasts.
4. Add phase-level resume and explicit per-stage token/evaluation budget accounting.
5. Add richer parent selection and novelty/archive policies only when experiments show a
   clear benefit.
6. Add additional problem-library and evaluator adapters behind the same data and
   evaluation boundaries.
7. Validate on a real application-backed DFO problem in addition to standard
   CUTEst/S2MPJ suites.

## P3: Public evolution explorer

Create a separate `optiprofiler-evolve-web` repository after the `agent_report`
schema and representative experiments are stable. The site should share the
visual identity of `www.optprof.com` and `app.optprof.com`, while serving as an
interactive, evidence-backed demonstration of DFO solver evolution rather than
a static marketing page.

1. Build a run explorer driven directly by the versioned `agent_report` and
   sanitized evolution traces; do not add a second website-only result schema.
2. Visualize iteration timelines, island populations, parent/candidate code
   changes, score and validation trajectories, and the benchmark evidence that
   informed each worker decision.
3. Publish a solver gallery containing only reproducible examples with fixed
   package commits, benchmark configurations, dependency policies, public /
   validation / hidden boundaries, source code, and downloadable artifacts.
4. Use real experiment state for motion and interaction. Pre-rendered overview
   plots, interactive profiles, and trace replay should carry the visual impact;
   decorative effects must not obscure the scientific evidence.
5. Start with a read-only explorer for selected runs. Online evolution belongs
   to a later security and operations milestone, not the first website release.

The intended deployment is `evolve.optprof.com`, subject to the final repository
and hosting decision.

The package should remain understandable: no additional top-level API, no generic
task framework, and no new subsystem without a tested responsibility boundary.

## P1.5: Harness architecture stabilization

- keep `evolve(...)` as the only top-level operation;
- express orchestration through ordered phases, one candidate attempt pipeline,
  and after-iteration policies rather than a general DAG;
- keep all population writes, accept/reject decisions, and authoritative events
  inside the engine;
- support worker and evaluator adapters without exposing executor internals;
- record a single-writer event ledger, component/config provenance, coordinate
  seeds, checkpoints, and a static local status view;
- make ablations immutable config transformations and preserve a mandatory
  engine-owned safety gate;
- require a new trusted component to need no framework-file edits, and a new
  built-in component to need only its implementation, registry entry, and test.

Implementation order for the stabilization pass:

1. Separate the controller-only event ledger from a centralized default-deny
   public projection, and remove provider/problem-library disclosures from the
   worker surface.
2. Preserve every provider-delivered worker/role byte incrementally, with
   immutable inputs, manifests, crash recovery, and derived readable traces.
3. Add the mandatory independent reviewer gate and explicit
   retention/sampling strategy seams, including scalar-compatible Pareto
   policies.
4. Complete worker/evaluator/network isolation and controlled research tools.
5. Render the public projection as a local Actions-style workflow/island view
   and export the same vocabulary through a single-job GitHub Actions wrapper.

Each item is independently tested and reviewed before work starts on the next
one. Changes remain inside this repository; OptiProfiler agent-output work is a
separate dependency and is not part of this implementation pass.
