"""Resolve experiment components at the application composition boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from optiprofiler_evolve.config import ComponentConfig, EvolveConfig, plain_data
from optiprofiler_evolve.controller.registry import resolve
from optiprofiler_evolve.observability.provenance import callable_identity


def component_provenance(
    config: EvolveConfig,
    *,
    worker_adapter: object,
    worker_provenance: Mapping[str, object],
    evaluator_factory: object,
    criterion_factories: Mapping[str, object],
    step_specs: Sequence[ComponentConfig],
) -> dict[str, Any]:
    # Config/hash preserve the declared experiment; components list constructed instances.
    # Unused attempt-step specs therefore remain in config, but not in this table.
    return {
        "phases": _component_table("phase", config.workflow.phases),
        "attempt_steps": _component_table("step", step_specs),
        "after_iteration": _component_table("policy", config.workflow.after_iteration),
        "retention": _component_table("retention", (config.evolution.retention,))[0],
        "parent_sampler": _component_table(
            "sampler", (config.evolution.parent_sampler,)
        )[0],
        "integrity_reviewer": _component_table(
            "reviewer", (config.reviewer_config().component,)
        )[0] if config.assessment.enabled("integrity") else None,
        "criteria": [
            {"name": name, **callable_identity(factory)}
            for name, factory in criterion_factories.items()
        ],
        "worker": {
            **callable_identity(worker_adapter),
            **dict(worker_provenance),
        },
        "evaluator": {
            "name": config.execution.adapter,
            "declared_deterministic": getattr(evaluator_factory, "deterministic", None),
            **callable_identity(evaluator_factory),
        } if evaluator_factory is not None else None,
    }


def _component_table(kind: str, specs: Sequence[ComponentConfig]) -> list[dict[str, Any]]:
    return [
        {
            "position": index,
            "name": spec.name,
            "options": plain_data(spec.options),
            **callable_identity(spec._factory or resolve(kind, spec.name)),
        }
        for index, spec in enumerate(specs)
    ]
