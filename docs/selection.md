# Island archives and selection

Each island owns a bounded archive. `evolution.population_per_island` is its
hard capacity. Two independent components govern it:

- **retention** chooses the ordered candidates kept after admission or
  migration;
- **parent sampler** chooses one parent from that retained order.

The controller validates component output. A retention policy cannot return an
unknown candidate, a duplicate, or more than the configured capacity. A sampler
cannot return a candidate outside its island archive. Only the controller applies
the result and writes the checkpoint.

## Default behavior

The defaults name, rather than change, the original alpha behavior:

```yaml
evolution:
  retention:
    name: validation_lexicographic
  parent_sampler:
    name: top_biased_validation_weighted
    options:
      greedy_ratio: 0.7
```

`validation_lexicographic` orders candidates by formal assessment score, public
score, iteration, and candidate ID — that whole tuple is
`CandidateRecord.ranking_key`. `top_biased_validation_weighted` chooses the first
candidate with probability `0.7`; otherwise it samples by nonnegative assessment
weight. With only performance weighted the assessment score equals the
performance validation score, so this reproduces the seeded baseline ordering;
seeded equivalence tests compare both components against the previous
implementation.

## Metric bundles

An `EvaluationResult` carries a controller-owned `MetricBundle`. Its primary
metric must equal `EvaluationResult.score`, preserving the existing scalar
contract. A bundle records:

- metric name, value, maximize/minimize direction, family, scope, and validity;
- a hash of the fixed experiment invariants;
- a deterministic metric-set ID derived from metric descriptors;
- optional opaque per-instance metrics and provenance.

Bundles are comparable only when their invariant hash and metric-set ID match.
A missing, non-finite, or incompatible selection metric fails closed. Metric
bundles are excluded from ordinary `EvaluationResult.as_dict()` output and are
not worker-visible feedback.

The evidence layers remain distinct: public bundles support diagnosis and
explicit research variants; validation/selection bundles drive the default
archive; hidden bundles are final-report evidence only.

## Pareto retention

`metric_pareto` performs deterministic nondominated-front retention for an
explicit list of two or more aggregate metrics. It uses the scalar default order
only to break a front at the capacity boundary.

```yaml
evolution:
  retention:
    name: metric_pareto
    options:
      objectives: [profile_performance, profile_data]
      epsilon: 0.0
```

With one objective and `epsilon` 0, `metric_pareto` does **not** delegate to
`validation_lexicographic`: it retains by the objective's own metric value, so it
can retain a different candidate whenever the assessed score and that metric
disagree. It creates no one-dimensional pseudo-front.
The package provides the interface before OptiProfiler's future agent-oriented
output exposes real profile/per-instance dimensions. Choosing unavailable
objectives quarantines the affected candidate instead of substituting zero or
silently dropping the metric.
