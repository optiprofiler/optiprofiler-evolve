# Changelog

## Unreleased

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

- Restore the worker task card text to its pre-refactor wording: the parent
  public fitness line, the "blind hyperparameter sweep" and benchmark-evidence
  phrasing, the evaluation-tool descriptions, and the required work order match
  the baseline prompt again (`solver-worker/5`). The benchmark-artifact reading
  paragraph also returns verbatim, now owned by the public performance standard
  and included whenever public performance checks exist, and the complexity
  standard again states its warn/enforce consequences. Research roles
  (direction-scout, strategy-analyst, strategy-integrator) are ordinary agent
  jobs again, so their prompts stay exactly as authored instead of receiving the
  worker-oriented criterion guidance.

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
