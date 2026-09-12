# Changelog

## Unreleased

- Share view presentation and iteration grouping without changing rendered output.
  Reuse validation scores between the owner index charts and attempt list.

- Resolve assessment profiles once per immutable configuration and reuse their
  settings for execution, guidance, and provenance. Record each enabled review
  profile and collect all reviewer worker versions instead of assuming validation.
- Fix shared competitor construction to use the base performance benchmark budget
  (500 when omitted), independent of profile overrides. Record the actual reference
  name, budget, and source identity in `controller/reference_manifest.json`.

- Validate raw worker output before explanation capture. Reject linked, special,
  or oversized explanation files before reading, and preserve unsafe output for
  diagnostics. Explanation JSON is limited to 1 MiB; ordinary malformed metadata
  remains non-blocking. EvalReport and explanation capture share bounded file I/O.

### Breaking changes from main

- Replace `evaluation` with `execution`; move performance options to
  `assessment.criteria.performance.options` and `integrity_review` to
  `assessment.criteria.integrity.options`. Legacy `evolution.rounds` and backend
  `local` are rejected; use `iterations` and `unsafe_local`.
- Replace feedback modes `summary`/`agent` with `score_only`/`full_artifacts`.
  The default remains score-only feedback.
- Quick-start defaults use 1 iteration, 1 island, population 2, no migration,
  and `mutate -> smoke -> public_evaluate`. Tool defaults are minimal, with no
  search or compilers. The default preset uses evaluation factor 20 (smoke 5),
  one benchmark process and score-only output. These values are unchanged from
  the previous release; the "substantially larger budgets" they replace predate
  that release, so do not read this entry as a change against the immediately
  preceding version.
- Remove `integrity_review.strict`: exhausted reviewer retries reject that
  candidate and exploration continues. This is not a fail-fast outage policy.
  Integrity is a zero-weight validation constraint by default, including the
  Python quick-start preset. Fully constructed criterion configs remain explicit.
- Configured additional positive-weight criteria change selection scores; their
  scales are not normalized. Warn disables constraint blocking, but missing
  positive-weight evidence still prevents admission. Complexity defaults to warn.
- Named competitors now honor bounds and constraints. Historical scores using
  these competitors are not directly comparable; reference `initial` is unchanged.
- An empty optional hidden split retains the validation score as the final score;
  it does not make another benchmark call or count as hidden evidence.

### Audit corrections

- Validate execution/data settings even without performance, restore research
  public-score tie breaking, report invalid direction artifacts, and keep optional
  view failures from aborting runs. Display each review gate's matching decision.
- Retain observed gateway usage after truncated streams or corrupt audit lines as
  explicitly labelled lower bounds; unknown exact totals remain unavailable.
- Bind formal public-artifact requirements from phase declarations, fix CI's
  competitor import and execute optional wrappers in the evaluator image.


- Align the integrity component's schema default with Python presets and validate
  worker pools before resolving reviewer defaults. Test preset schema round trips.
- Keep feedback/index generation solely in worker projection and remove the
  unreachable legacy agent-report interpretation branch.
- Inject criterion guidance classes and reviewer factories instead of resolving
  controller registries inside criteria. Bind provenance identities and prompt
  versions in application, and move matrix orchestration there with an explicit
  run callable. Matrix cancellation now stops subsequent cells without losing
  completed results. Guard the dependency directions in tests.

- Merge the upstream EvalReport consumer and move it with its pinned schemas into
  observability. Validate report/companion integrity, bounded file reads, references,
  history bins and numeric axes without changing fitness or enabling report production.
- Exclude raw EvalReports and receipt-named companions from full worker feedback;
  unusable receipts withhold additional artifacts without losing completed scores
  or spending another evaluation call. Diagnostics remain controller-private,
  and broker errors no longer echo private exception paths to workers.
- Separate EvalReport I/O boundaries from protocol validation, distinguish
  unsupported versions, and centralize bundled schema digests. Receipt discovery
  does not require full numerical validation. Accept retained-archive coverage
  and declared axis subsets without imposing unsupported completeness rules.

- Preserve successful unscored matrix cells and report their count separately.
  Bind core contexts by implementation type, reject duplicate artifact producers,
  and retain phase-supplied failure reasons in exceptions and events.
- Restore result copying and hashing with read-only artifacts. Persist hidden-query
  consumption before execution and reject inconsistent prepared island counts.

- Workflows may omit core phases or be empty. Application now returns
  `EvolveResult` independently of report, with nullable winner/score fields and
  a read-only `artifacts` mapping; `without_phase` can remove core stages.
- Reject configured optional-input producers placed after their consumers before
  spending run budget. Absent optional producers remain valid. Phase assembly
  validation and optional extension adapters now live in `phases/contracts.py`.
- Core phase artifacts and run-state views derive from committed typed results,
  eliminating separately writable copies of champion and final-result facts.
  Remove redundant hidden identity and champion-hash checks, validate the actual
  returned solver tree, and apply one lockbox policy to planned core stages and
  committed results. Unused attempt steps remain unconstructed.

- Preserve the baseline parent-public-fitness wording, algorithmic improvement
  instructions, and initial five work steps. The current task additionally
  requires explanation JSON, whose summary can enter later worker memory; these
  are behavior changes beyond complexity and EvalReport. Explorer and integrator
  now share a complete explanation contract (`solver-worker/6`,
  `strategy-transfer/2`), including explicit `file`/`lines` bindings and a 1 MiB
  limit. Both are SolverJobs and receive criterion guidance. Scout, analyst, and
  reviewer jobs retain their independent prompts. Performance guidance describes
  `pdftoppm` and conditional inspection of available public artifacts; it is not
  verbatim baseline text and does not grant an integrator evaluation tools.

- The fixed seed now receives a controller-local baseline integrity approval
  (the seed equals its parent and has no mutation transcript), so prepare no
  longer invokes the reviewer agent and a reviewer outage cannot abort a run
  before exploration; reviewer budget is spent only on actual submissions.

- Removed the never-functional `execution.feedback_mode=structured_numeric`
  (no internal `agent_report/1` producer or validator): the mode is gone from
  config, schema, projection code and docs, and an unscored evaluation can no
  longer silently fall through to the `full_artifacts` publication branch.
- Removed the un-wired `complexity_checked` event path (events, public
  allowlist, run-state and page rendering) that no production code emitted; the
  public/owner pages no longer promise a live "Latest source complexity" block.
- Unify public event allowlists on one source (`PUBLIC_TRACE_COVERAGE_KEYS`),
  add the emitted `worker_cancelled`/`cancelled` fields to public attempt and
  worker projections, and drop never-emitted `trace_*` allowlist keys.
- Assessment runners now hash the candidate snapshot once before and once after
  the criterion loop instead of before and after every criterion.
- Feedback-mode fall-through, dead `_freeze_value` private cross-package import,
  broker double candidate validation, reports finalist ordering and a duplicate
  supported-strategy count are fixed; the owner CSS/JS assets are extracted to
  `observability/views/_owner_assets.py`, and empty `__all__` tails are removed.

- Owner console workflow is now a continuous pan/zoom canvas (Fit, zoom in,
  zoom out; drag or scroll to pan) so long research pipelines keep unbroken
  connectors at every width; the sanitized public page stays a script-free
  scrollable strip. Every phase node links to a private
  `owner/phases/<phase>.html` page derived from the event ledger and run
  evidence: status and duration, configured options from provenance, phase
  roles, evidence links, and a bounded phase-event table. The explore node
  shows an islands/iterations/attempts/accepted/quarantined summary and its
  page carries the full island-by-iteration matrix plus the effective
  population-policy mode (metric_pareto reports its scalar fallback when
  only one objective is configured).

- Turn the run-directory root `status.html` into the PRIVATE owner console:
  every attempt, integrity-review invocation, and research-role job links to a
  static per-invocation evidence page (transcript and tool calls, bounded
  stdout/stderr previews, source diff, benchmark artifacts, reviewer findings,
  gateway outcome, owner-only validation/hidden results). The sanitized public
  page is now rendered to `public_status.html` and published unchanged as
  `public/status.html` through an explicit source-to-target bundle allowlist.
- Add a derived owner evidence manifest plus `scripts/pack_owner_evidence.py`
  for explicit per-attempt/per-job packaging with symlink and path-traversal
  protection. Nothing owner-side is ever uploaded by the Actions templates.
- Add the external algorithm-repository GitHub Actions example
  (`examples/external-repository/`) with a pinned package fetch outside the
  solver tree.

- Keep the controller event ledger private and derive all shareable status data
  through a centralized default-deny event projection.
- Add a versioned public run-state projection, an Actions-style server-free
  workflow/island view, a sanitized Markdown summary, and an exact allowlisted
  `public/` bundle for the single-job GitHub Actions template.
- Remove problem-library identity, reference raw scores, and scoring-formula
  details from worker manifests and evaluation responses.
- Preserve built-in CLI worker and research-role stdout/stderr incrementally in
  private per-invocation traces, including partial output on timeout, while
  keeping readable transcripts as derived artifacts.
- Add trace invocation/outcome manifests, process-group timeout cleanup,
  cooperative SIGINT/SIGTERM cancellation, idempotent interrupted-run recovery,
  and a controller-owned fallback for injected worker adapters.
- Route Docker worker model traffic through a pinned per-invocation provider
  sidecar, with separate internal/egress networks, credential separation,
  metadata-only request audit, terminal recovery, and labeled best-effort GC.
- Make island retention and parent sampling explicit registered components while
  preserving the seeded legacy default, and add validated controller-only metric
  bundles plus scalar-compatible Pareto retention.
- Add a mandatory pre-validation semantic integrity reviewer with strict JSON
  evidence, retry/quarantine behavior, separate model budgets, and complete
  private reviewer traces.
- Keep remote model-API transport enabled for strategy-analysis agents while
  leaving their web-search tools disabled by default.
- Preserve nested component options as structured JSON in provenance and run
  events.
- Pair the PRIMA challenger example with an auditable candidate-import ban.
- Add an auditable `forbidden_candidate_imports` experiment policy for
  external-solver-API ablations.
- Require agent-feedback workers to inspect selected benchmark plots through a
  raster-image tool before their final edit.

- Added collaborator onboarding, complete public API and YAML configuration
  references, editor JSON Schema, and runnable Claude/Codex/multi-file examples.
- Documented the planned host-MATLAB evaluator and its candidate/reference path
  isolation boundary.
- Added tests that keep config dataclasses, schema, reference docs, and example
  YAML files synchronized.
- Fixed Docker prompt delivery and Claude Code stream-JSON compatibility, and
  added a tested Anthropic-compatible provider example.
- Give every worker a dedicated temporary Docker network. Gateway-routed
  workers have provider transport and harness-native search without general
  shell egress; direct network access remains an explicit unsafe ablation.
- Reduced quick-start container defaults and bounded the example solvers so
  local worker tests cannot wait indefinitely.

## 0.1.0a0

- Rewritten from scratch as an OptiProfiler-specific solver evolution package.
- Added strict experiment configuration and the single `evolve(...)` API.
- Added repository candidates, deterministic data splits, Python benchmark
  evaluation, Codex/Claude workers, Docker isolation, islands, checkpoints, and
  final hidden reranking.
