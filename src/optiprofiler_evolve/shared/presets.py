"""Small, explicit experiment presets."""

from __future__ import annotations

import os

from optiprofiler_evolve.shared.config import (
    DataConfig,
    EvolveConfig,
    EvaluationConfig,
    EvolutionConfig,
    IntegrityReviewConfig,
    ProviderGatewayConfig,
    SandboxConfig,
    ToolConfig,
    WorkerConfig,
    WorkersConfig,
)


def dfo_default(*, worker: WorkerConfig | None = None) -> EvolveConfig:
    """Return the default isolated DFO evolution configuration.

    The preset is only a constructor.  Callers can derive immutable variants
    with :class:`EvolveConfig` helper methods before passing it to ``evolve``.
    """

    selected = worker or WorkerConfig(
        harness="codex",
        model=os.environ.get("OPTIPROFILER_EVOLVE_MODEL", "gpt-5.3-codex"),
        pass_env=("OPENAI_API_KEY",),
        provider_gateway=ProviderGatewayConfig(
            upstream_base_url="https://api.openai.com/v1",
            credential_env="OPENAI_API_KEY",
        ),
    )
    return EvolveConfig(
        data=DataConfig(selection={"ptype": "u", "mindim": 1, "maxdim": 5}),
        evaluation=EvaluationConfig(
            backend="docker",
            docker_image="optiprofiler-evolve-evaluator:latest",
            benchmark={"max_eval_factor": 20, "n_jobs": 1, "score_only": True},
            smoke_overrides={"max_eval_factor": 5},
        ),
        evolution=EvolutionConfig(
            iterations=1,
            islands=1,
            attempts_per_island=1,
            population_per_island=2,
            migration_interval=0,
            finalists_per_island=1,
        ),
        workers=WorkersConfig(
            pool=(selected,),
            max_parallel=1,
            tools=ToolConfig(),
        ),
        integrity_review=IntegrityReviewConfig(allow_same_model=True),
        sandbox=SandboxConfig(
            backend="docker",
            worker_image="optiprofiler-evolve-worker:latest",
        ),
    )


__all__: list[str] = []
