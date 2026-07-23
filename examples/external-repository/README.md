# External solver repository on GitHub Actions

This example shows how a separate algorithm repository can run
`optiprofiler-evolve` without copying the package into the candidate solver.

## Repository layout

Copy the example files into these locations:

```text
my-dfo-solver/
  solver/
    solver.py
  evolve/
    experiment.yaml
    run.py
  .github/workflows/
    evolve.yml
```

`solver/` is the candidate root copied for each worker. Put every file that the
solver may edit below that directory. The checked-in example uses
`solver.py:solver`; change `interface` in `evolve/run.py` if the entry point is
elsewhere.

## Repository configuration

Create:

- repository variable `OPTIPROFILER_EVOLVE_MODEL`;
- Actions secret `ANTHROPIC_API_KEY`.

The example uses the standard Anthropic endpoint and Claude Code harness. For a
compatible endpoint or Codex worker, replace `evolve/experiment.yaml` with the
closest provider example from the package and configure the matching secret.

The workflow pins an exact `optiprofiler-evolve` commit in
`OPTIPROFILER_EVOLVE_REF`. Update that value only after testing the newer
revision, and replace the commit pin with a release tag once one is published.
The source checkout lives under `$RUNNER_TEMP`, outside the algorithm
repository and outside the solver candidate.

The example config sets `integrity_review.allow_same_model: true` so the demo
runs with a single provider account. That collapses reviewer independence and
is acceptable only for a cheap demo; real experiments should review with a
worker outside the mutation pool.

Launch the workflow manually from **Actions > OptiProfiler Evolve > Run
workflow**. The run produces:

- a sanitized GitHub Job Summary;
- `optiprofiler-evolve-public-<run-id>`, containing only `run_dir/public/`.

The complete run directory contains private candidates, validation and hidden
results, reviewer findings, provider evidence, and raw traces. This example does
not upload it.

Keep `workflow_dispatch` and run only trusted branches. In particular, do not
trigger this credentialed workflow from pull requests submitted by untrusted
contributors, and do not attach an unrestricted self-hosted runner to such a
workflow.
