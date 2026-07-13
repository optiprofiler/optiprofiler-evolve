# Changelog

## Unreleased

- Added collaborator onboarding, complete public API and YAML configuration
  references, editor JSON Schema, and runnable Claude/Codex/multi-file examples.
- Documented the planned host-MATLAB evaluator and its candidate/reference path
  isolation boundary.
- Added tests that keep config dataclasses, schema, reference docs, and example
  YAML files synchronized.

## 0.1.0a0

- Rewritten from scratch as an OptiProfiler-specific solver evolution package.
- Added strict experiment configuration and the single `evolve(...)` API.
- Added repository candidates, deterministic data splits, Python benchmark
  evaluation, Codex/Claude workers, Docker isolation, islands, checkpoints, and
  final hidden reranking.
