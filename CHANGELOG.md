# Changelog

## Unreleased

### Stabilization follow-up

- Add explicit, double-opt-in Codex subscription authentication over the existing
  Docker/Harbor worker boundary. Preserve each Harbor launch receipt on retries.
- Require source-matching quotes for agent reviewer findings and provide a
  canonical parent-to-candidate diff to avoid reversed-diff false rejections.
- Clarify executable ablation and strategy-transfer output contracts. Required
  final explanations can be repaired in a bounded, isolated metadata-only job;
  failed delivery is explicit and never deletes the selected solver.
- Align the JSON Schema with performance-disabled assessment configurations.
- Preserve native CLI token accounting without inventing API-call counts or
  subscription costs. Associate propagated oracle errors with their exact callback.
- Bound Docker numeric cleanup and verify asynchronous removal before declaring
  failure. Reject obvious worker/scorer image reuse as the numeric solver image.

### Breaking changes and migration

- Replace top-level `evaluation` with `execution`. Performance options move to
  `assessment.criteria.performance.options` and `integrity_review` moves to
  `assessment.criteria.integrity.options`. `assessment.criteria` is the
  **complete** criterion map rather than a patch, so a criterion left out does not
  run at all; omitting `integrity` drops the review gate. The loader warns by
  name for every default-enabled criterion that is missing. The loader error
  prints a complete starting block.
- Remove the `structured_numeric` feedback mode. It required an `agent_report/1`
  validator that never shipped, so the mode could only fail closed. An unscored
  evaluation can no longer fall through to the `full_artifacts` branch.
- Remove the `evolution.rounds`, `feedback_mode: summary|agent` and
  `backend: local` aliases. Use `iterations`, `score_only`/`full_artifacts` and
  `unsafe_local`. Note `summary`/`agent` were never accepted mode values, only
  translated aliases.
- Remove `integrity_review.strict`. Exhausted reviewer retries reject that
  candidate and exploration continues; this is not a fail-fast outage policy.
- `experiment_fingerprints.json` moves to `schema_version` 2. The hashing code is
  unchanged but it hashes the resolved configuration, whose key space changed, so
  fingerprints recorded before this release do not compare with new ones.
- Named competitors now honor bounds and constraints, so historical scores using
  them are not directly comparable. Reference `initial` is unchanged.
- `metric_pareto` with a single objective no longer delegates to
  `validation_lexicographic`; it retains by the objective's metric value and can
  therefore retain a different candidate when that metric and the assessed score
  disagree.
- The adaptive `evo` provider route is removed in favour of an explicit endpoint.
- Quick-start defaults are unchanged from the previous release: 1 iteration,
  1 island, population 2, no migration, `mutate -> smoke -> public_evaluate`,
  evaluation factor 20 and smoke 5. Earlier releases used larger search budgets
  and tool sets; read that difference against those releases, not this one.

### Added

- Add the `complexity` assessment criterion: a static, baseline-relative source
  budget measured with radon in an isolated subprocess that never imports the
  candidate. It is disabled by default and, when enabled with a positive weight,
  contributes to the selection score.
- Integrate the OptiProfiler **EvalReport** consumer into `observability`, with
  pinned companion schemas. It validates report/companion integrity, bounded file
  reads, cross-references, history bins and numeric axes. It never supplies
  fitness and enables no report production.
- Exclude raw EvalReports and receipt-named companions from `full_artifacts`
  worker feedback. An unusable receipt withholds additional artifacts while
  preserving the completed score, and spends no further evaluation.
- Custom workflows: `workflow.phases` may be any ordered list, including an empty
  one, and `without_phase` can remove core stages. `EvolveResult` is constructed
  independently of the report, with nullable winner/score fields and a read-only
  `artifacts` mapping.

### Behavior changes

- Assessment profiles resolve once per immutable configuration and are reused for
  execution, generation guidance and provenance.
- Bind formal public-artifact requirements from phase declarations. A workflow
  that includes `strategy_analysis` while `feedback_mode` is `full_artifacts` now
  runs attempt public evaluations in `public` mode instead of `public_score`.
- The fixed seed receives a controller-local baseline integrity approval, because
  it equals its parent and has no mutation transcript. Reviewer budget is spent
  only on real submissions, and a reviewer outage cannot abort a run before
  exploration.
- Reject configured optional-input producers placed after their consumers before
  spending run budget. Absent optional producers remain valid.
- Persist hidden-query consumption before execution, and restore result copying
  and hashing with read-only artifacts.
- Share competitor construction on the base performance benchmark budget (500 when
  omitted), independent of profile overrides. Record the reference name, budget and
  source identity in `controller/reference_manifest.json`.
- Validate raw worker output before explanation capture: reject linked, special or
  oversized explanation files before reading. Explanation JSON is capped at 1 MiB
  and malformed metadata stays non-blocking.
- Core phase artifacts and run-state views derive from committed typed results, so
  champion and final-result facts are no longer separately writable. `RunState`
  and `RunCommits` are the only registry and writer. Hidden identity and
  champion-hash duplication is gone, the returned solver tree is validated, one
  lockbox policy covers planned and committed stages, and unused attempt steps are
  not constructed.
- Failed/cancelled phase results are never committed. Successful and skipped
  phases must publish exactly their declared outputs, and preflight checks unique
  resolved phase names, unique artifact producers and inputs against preceding
  outputs before any run directory exists.
- Restore research public-score tie-breaking and report invalid direction
  artifacts. Optional view failures no longer abort a run.
- Retain observed gateway usage after truncated streams or corrupt audit lines as
  explicitly labelled lower bounds. Unknown exact totals stay unavailable.
- Preserve successful unscored matrix cells and report their count separately.
  Matrix cancellation stops subsequent cells without discarding completed results.
- Attempt public evaluations get suffixed directories when one attempt evaluates
  more than once, instead of overwriting the previous result.

### Removed

- Remove the un-wired `complexity_checked` event path and its page rendering; no
  production code ever emitted it, and the public page no longer promises a live
  source-complexity block.
- Remove never-emitted `trace_*` public allowlist keys. Add the emitted
  `worker_cancelled`/`cancelled` fields to the public attempt and worker
  projections, and unify the allowlists on `PUBLIC_TRACE_COVERAGE_KEYS`.

### Internal

Not user-visible; listed because they change module locations that external code
may import.

- The 2692-line `engine.py` is replaced by `application/`, `controller/`,
  `criteria/{performance,complexity,integrity}/`, `observability/{views,schemas}/`
  and per-phase modules under `phases/`.
- `presets` is now `_presets`; `references`, `fingerprints`, `protocols`,
  `feedback` and `review` are gone, with their contents relocated.
- `agents` owns the worker harness, sandbox, broker and LLM entrypoint.
  `UsageRecord` and the worker-exit projection live in the package root because
  both `agents` and `observability` need them, which otherwise made those two
  packages mutually dependent.
- Add `tests/test_import_boundaries.py`, which enforces the dependency directions
  declared in `docs/architecture-constitution.md` by reading imports with `ast`.

## 0.1.0a0

- Rewritten from scratch as an OptiProfiler-specific solver evolution package.
- Added strict experiment configuration and the single `evolve(...)` API.
- Added repository candidates, deterministic data splits, Python benchmark
  evaluation, Codex/Claude workers, Docker isolation, islands, checkpoints, and
  final hidden reranking.
