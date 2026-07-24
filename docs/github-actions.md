# GitHub Actions

The v1 integration is a template, not a second scheduler. One GitHub Actions
job calls the repository's experiment script. Inside that process,
`evolve(...)` still owns phases, islands, attempts, reviewer gates, selection,
checkpoints, and cancellation.

Start with [`examples/github-actions/evolve.yml`](../examples/github-actions/evolve.yml)
and its paired [`run.py`](../examples/github-actions/run.py):

1. Copy the workflow to `.github/workflows/evolve.yml` in the experiment
   repository.
2. Adapt `examples/github-actions/run.py` to the solver, interface, editable
   paths, config, and fixed run directory.
3. Store the model ID as a repository variable and the provider credential as
   an Actions secret. Map the credential only on the experiment step.
4. Keep the workflow manually triggered until the evaluation budget and model
   cost are understood.
5. Inspect the Job Summary and download the public artifact after the job.

The template has one job by design. Splitting phases or islands into GitHub jobs
would create a second orchestration model with different resume, cancellation,
and state semantics.

The files above are the package-repository example. For a separate algorithm
repository, use
[`examples/external-repository/`](../examples/external-repository/README.md).
That workflow fetches a pinned package revision into `$RUNNER_TEMP`, builds the
three images from that clean source tree, and runs the experiment script stored
with the solver. Merely copying `examples/github-actions/evolve.yml` into an
algorithm repository is insufficient because `pip install -e .` and the Docker
build contexts in that file deliberately refer to this package repository.

## Deterministic CI versus credentialed E2E

The repository CI is fully deterministic and free of provider credentials: it
never calls a paid model and defines no provider secrets. The `python` job runs
the complete unit suite — including the strategy-analysis contract tests that
script an analyst for the verified-ablation path, the declared
`not_decomposable` path, and the analyst-failure path — with stub agent
runners. The `docker` job builds the three runtime images, probes worker
container isolation and the gateway transport against a fake in-network
upstream, and scores a seed candidate inside the real evaluator container on a
single problem with opaque naming (`OPE_RUN_DOCKER_TESTS=1`).

Real end-to-end evolution runs — actual coding models through the provider
gateway — are owner-run only, with credentials supplied outside version
control. CI must never grow a step that requires a provider secret; anything
needing one belongs in an owner-launched run, not in this workflow.

## Public artifact boundary

The `always()` steps read
`<run_dir>/public/PUBLIC_REPORT.md` and upload `<run_dir>/public/`. The
controller rebuilds that directory from this exact filename allowlist:

```text
public_events.jsonl
public_run_state.json
status.html
report.html                    # once available
public_trace_coverage.json     # once available
PUBLIC_REPORT.md
```


Do not change the upload path to `<run_dir>`. The full directory contains
controller-only validation and hidden results, reviewer findings, provider
evidence, raw traces, candidates, workspaces, and the private scientific
`FINAL_REPORT.md`.

The status page and Job Summary deliberately report workflow state, public
candidate fitness, and aggregate trace coverage only. A run owner may inspect
the private artifacts on the trusted runner or move them to separately governed
private storage.

## Current template scope

The checked-in template uses the Claude Code example on a GitHub-hosted Linux
runner and builds all three Docker images. Adapt the provider mapping for Codex
or a compatible endpoint as described in [Model providers](providers.md).
Long or costly studies should use a controlled self-hosted runner and explicit
job timeout, concurrency, model budget, and artifact-retention settings.

The template pins each third-party action to a full release commit. Reusable
workflows or a composite action can be considered after the package has a
stable published release; they are intentionally absent from the alpha.
