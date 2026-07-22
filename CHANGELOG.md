# Changelog

## Unreleased

- Keep the controller event ledger private and derive all shareable status data
  through a centralized default-deny event projection.
- Remove problem-library identity, reference raw scores, and scoring-formula
  details from worker manifests and evaluation responses.
- Preserve built-in CLI worker and research-role stdout/stderr incrementally in
  private per-invocation traces, including partial output on timeout, while
  keeping readable transcripts as derived artifacts.
- Add trace invocation/outcome manifests, process-group timeout cleanup,
  cooperative SIGINT/SIGTERM cancellation, idempotent interrupted-run recovery,
  and a controller-owned fallback for injected worker adapters.
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
- Gave every worker a dedicated temporary Docker network while preserving
  outbound research access, and added `ddgr` as a provider-independent web
  search fallback.
- Reduced quick-start container defaults and bounded the example solvers so
  local worker tests cannot wait indefinitely.

## 0.1.0a0

- Rewritten from scratch as an OptiProfiler-specific solver evolution package.
- Added strict experiment configuration and the single `evolve(...)` API.
- Added repository candidates, deterministic data splits, Python benchmark
  evaluation, Codex/Claude workers, Docker isolation, islands, checkpoints, and
  final hidden reranking.
