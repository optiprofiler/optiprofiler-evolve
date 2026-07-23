# Changelog

## Unreleased

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
