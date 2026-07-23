"""Small, explicit experiment presets."""

from __future__ import annotations

import os

from .config import (
    EvolveConfig,
    EvaluationConfig,
    ProviderGatewayConfig,
    SandboxConfig,
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
        evaluation=EvaluationConfig(
            backend="docker",
            docker_image="optiprofiler-evolve-evaluator:latest",
        ),
        workers=WorkersConfig(pool=(selected,)),
        sandbox=SandboxConfig(
            backend="docker",
            worker_image="optiprofiler-evolve-worker:latest",
        ),
    )


__all__: list[str] = []
