# GitHub Actions template

This directory demonstrates one evolution run as one GitHub Actions job. Copy
`evolve.yml` to `.github/workflows/evolve.yml`, then adapt `run.py`, the model
variable, and the provider secret to the experiment repository.

The workflow deliberately does not turn phases or islands into GitHub jobs.
`evolve(...)` remains the scheduler; GitHub Actions launches it, displays the
sanitized `PUBLIC_REPORT.md` as the Job Summary, and uploads only
`runs/github-actions/public/`.

Never change the upload path to the whole run directory. The rest of the run
contains controller-only validation and hidden results, reviewer findings, raw
agent traces, provider evidence, candidates, and workspaces.

The checked-in template uses the Claude Code example. Store the model ID in the
repository variable `OPTIPROFILER_EVOLVE_MODEL` and the provider credential in
the Actions secret `ANTHROPIC_API_KEY`. For Codex or a compatible endpoint,
replace the experiment config and map only the credential required by that
provider. See [Model providers](../../docs/providers.md).
