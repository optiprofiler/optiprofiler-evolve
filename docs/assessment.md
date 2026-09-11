# Algorithm assessment

Every quality standard implements `Criterion.assess(AssessmentRequest)` and
returns independent execution status, constraint verdict, score, metrics and
evidence. The built-ins are `performance`, `complexity` and `integrity`.
All three can be disabled. Filesystem safety, sandbox isolation and edit
permissions remain execution boundaries, not optional scoring criteria.

## Generation guidance

Each criterion also implements the class method
`generation_guidance(GuidanceRequest)`. Performance owns its optimization and
benchmark-reading instructions; complexity owns its simplification rules;
integrity owns its anti-gaming guidance. The stage task contains no
criterion-specific prompt slots or unconditional performance-artifact demands.

`SolverJob`, a subtype of `AgentJob`, identifies solver proposal/edit tasks.
`LLMClient` combines their task text with controller-bound criterion guidance
exactly once and records that effective prompt in the invocation trace. Scout,
analysis, transfer and explorer jobs share this path. Independent review jobs
remain ordinary `AgentJob` instances and never request generation guidance.
This does not add an LLM call to performance or complexity assessment.

Guidance describes the enabled smoke/public rules and publicly advertised
validation rules, including weights and warn/enforce settings. Identical rules
are grouped; different profile overrides remain explicit. Disabled standards
and hidden-only settings do not contribute instructions. Performance plot
instructions appear only when public performance and full artifact feedback
are enabled and this invocation can call `evaluate`. Tool instructions use the
controller-bound invocation capabilities; roles without a broker still receive
the scoring rules, but no instructions to call evaluation tools or inspect their
artifacts. A zero call budget omits that tool from the guidance.

The guidance request carries only profiles, weight, mode, feedback format,
available evaluation tool names and
explicitly allowlisted option fields. `Criterion.guidance_fields` defaults to
empty; built-ins expose only `fitness_source` or `max_ratio`, never reviewer
configuration or benchmark input mappings. Custom criteria must implement the
guidance method and explicitly declare any public rule fields. No criterion is
instantiated or evaluated to generate instructions. Candidate/reference paths,
private results, problem identities, hidden settings and credentials are not
guidance inputs. Numeric complexity diagnostics remain public-tool evidence,
not a second source-analysis pass performed while building a prompt.

## Configuration

```yaml
assessment:
  criteria:
    performance:
      enabled: true
      weight: 0.7
      mode: enforce
      options:
        benchmark: {max_eval_factor: 100}
    complexity:
      enabled: true
      weight: 0.2
      mode: warn
      options: {max_ratio: 1.2}
    integrity:
      enabled: true
      weight: 0.1
      mode: enforce
      options: {allow_same_model: true}
  profiles:
    smoke:
      performance:
        options:
          benchmark: {max_eval_factor: 10}
    validation:
      complexity:
        weight: 0.3
        mode: enforce
```

Use a dedicated reviewer worker for scientific runs; `allow_same_model` is an
explicit experiment choice. If `criteria` is supplied, it is the complete
criterion map, not a patch to defaults. Omitted criteria do not run. Without an
assessment block, performance has weight 1 and integrity is an enforced,
zero-weight constraint; complexity is disabled. An integrity mapping without a
weight also defaults to 0. Set a positive integrity weight explicitly when its
score should influence selection. Integrity defaults to validation only. Other criteria
default to all four profiles. Profile overrides can change enabled, weight,
mode and options; options merge by key, not recursively. A global disabled
criterion cannot be re-enabled by a profile.

## Scores and constraints

Scores are finite real numbers, with higher meaning better. Neither individual
scores nor the total are clipped to `[0, 1]`; negative values and scores above 1
are supported. Each criterion owns its scale. Weights control its influence:

```text
total = sum(weight * score) / sum(weight)
```

Weights must be finite and nonnegative; they need not sum to one. For example,
scores 2, 3, 1 and weights 7, 2, 1 produce 2.1, not 1. A zero-weight criterion
still runs but contributes only diagnostics or an enforced constraint.

Performance preserves the existing OptiProfiler fitness reduction. Complexity
uses `1 / (1 + r)`, where `r` is the larger initial-relative branch/line ratio;
the threshold `max_ratio` is independent of this score. Integrity maps approve
to 1 and quarantine to 0. These are built-in formulas, not framework limits.
The seed equals its parent and has no mutation transcript, so its integrity
check is a controller-local baseline approval and never invokes the reviewer
model; only actual submissions consume reviewer budget.

`enforce` rejects unmet constraints. `warn` permits the candidate to continue,
but the score still contributes with its configured weight. A failed
positive-weight measurement leaves the total unavailable: it is never removed
from the denominator or silently replaced with zero. Zero-weight failures warn
or reject according to mode. Formal assessment needs at least one enabled,
positive-weight criterion; invalid configurations fail before workers start.
An initial solver that cannot complete formal assessment fails preparation.

Population retention, configured parent sampling and champion selection use the
formal weighted score. The weighted sampler derives nonnegative sampling
weights with `max(total, 1e-6)`; it does not rewrite stored scores. Ranking uses
the unmodified total, then available public performance, then stable lineage/ID
tie breaks. With complexity disabled and default integrity settings, the score
and seeded parent sampling retain the original performance-only behavior. No criterion toggle
implicitly switches the selection algorithm. Explicit Pareto research policies
continue to require their configured raw metric bundles.

## Checkpoints and privacy

| Profile | Invocation | Authority |
|---|---|---|
| `smoke` | Worker tool and submission smoke step | Public data only |
| `public` | Public tool, submission check, research variants | Public data only |
| `validation` | Seed and candidate admission, research finalist admission | Controller only |
| `hidden` | Once after fixing the champion | Controller only; never selects a replacement |

Public checks precede formal admission. Within an assessment, source constraints
and integrity review precede performance execution. Formal results are tied to
the evaluated snapshot. Champion selection reuses those results; hidden results
never feed back. When hidden has no enabled measurement it is recorded as
skipped, not a successful hidden-set execution.

Every assessment writes `assessment.json` alongside its private evidence,
including raw scores, weights, versions, input/config fingerprints and total.
Disabled or failed measurements have no score. Raw performance and weighted
assessment scores are separate fields. Public feedback exposes only public
profile statuses and scores, not private reports, paths or validation totals.

Disabling performance avoids loading the problem library or constructing its
evaluator. Research phases that explicitly need public benchmarks must be
disabled too; dependency conflicts are reported before execution. Disabling
integrity needs no reviewer model and emits no fabricated approve verdict.

## Migration and extension

Move `evaluation` resource/backend/feedback/quota fields to `execution`.
Move benchmark, smoke overrides, reference and fitness source to
`assessment.criteria.performance.options`. Move `integrity_review` to
`assessment.criteria.integrity.options`. Move complexity `mode` to the criterion
entry and `max_ratio` to its options, with `enabled: true`. Old top-level keys
are rejected with a migration hint.

Trusted extensions subclass `Criterion` and implement `assess` plus the class
method `generation_guidance`. Register explicitly in the `criterion` registry.
Assessment receives the controller-bound snapshot and configured options;
generation receives only the public rule projection described above.
Registration never discovers packages automatically. Existing execution
adapters remain separate from criteria.
