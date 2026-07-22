"""Stable experiment identity and best-effort environment capture."""

from __future__ import annotations

import hashlib
import inspect
import json
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from functools import partial
from importlib import metadata
from pathlib import Path
from typing import Any

from .config import ComponentConfig, EvolveConfig, plain_data
from .prompt import WORKER_PROMPT_VERSION
from .registry import resolve


def coordinate_seed(base_seed: int, *coordinates: object) -> int:
    """Derive independent random streams that do not shift under ablations."""

    payload = json.dumps([base_seed, *coordinates], separators=(",", ":"), default=str)
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def build_run_provenance(
    config: EvolveConfig,
    *,
    worker_adapter: object,
    worker_provenance: Mapping[str, object],
    evaluator_factory: object,
) -> dict[str, Any]:
    """Build the redacted identity written into the first run event."""

    resolved = config.resolved_dict()
    encoded = json.dumps(resolved, sort_keys=True, separators=(",", ":"), default=str)
    components = {
        "phases": _component_table("phase", config.workflow.phases),
        "attempt_steps": _component_table("step", config.workflow.attempt_steps),
        "after_iteration": _component_table("policy", config.workflow.after_iteration),
        "retention": _component_table("retention", (config.evolution.retention,))[0],
        "parent_sampler": _component_table(
            "sampler", (config.evolution.parent_sampler,)
        )[0],
        "worker": {
            **_callable_identity(worker_adapter),
            **dict(worker_provenance),
        },
        "evaluator": {
            "name": config.evaluation.adapter,
            "declared_deterministic": getattr(evaluator_factory, "deterministic", None),
            **_callable_identity(evaluator_factory),
        },
    }
    return {
        "schema_version": "1",
        "config": resolved,
        "config_hash": hashlib.sha256(encoded.encode()).hexdigest(),
        "components": components,
        "environment": {
            "package_version": _package_version(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "worker_image": _image_identity(
                config.sandbox.worker_image if config.sandbox.backend == "docker" else None
            ),
            "evaluator_image": _image_identity(
                config.evaluation.docker_image if config.evaluation.backend == "docker" else None
            ),
        },
        "seed_coordinates": ["seed", "phase", "iteration", "island", "attempt", "component"],
        "prompt_templates": {"solver_worker": WORKER_PROMPT_VERSION},
    }


def _component_table(kind: str, specs: Sequence[ComponentConfig]) -> list[dict[str, Any]]:
    return [
        {
            "position": index,
            "name": spec.name,
            "options": plain_data(spec.options),
            **_callable_identity(spec._factory or resolve(kind, spec.name)),
        }
        for index, spec in enumerate(specs)
    ]


def _callable_identity(value: object) -> dict[str, Any]:
    bound: dict[str, object] | None = None
    if isinstance(value, partial):
        bound = {
            "args": list(value.args),
            "keywords": dict(value.keywords or {}),
        }
        value = value.func
    target = value if inspect.isclass(value) or inspect.isfunction(value) else value.__class__
    module = getattr(target, "__module__", type(value).__module__)
    qualname = getattr(target, "__qualname__", type(value).__qualname__)
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        source_hash = None
    else:
        source_hash = hashlib.sha256(source.encode()).hexdigest()
    source_file = inspect.getsourcefile(target)
    try:
        source_file_hash = (
            hashlib.sha256(Path(source_file).read_bytes()).hexdigest() if source_file else None
        )
    except OSError:
        source_file_hash = None
    identity = {
        "callable": f"{module}:{qualname}",
        "source_hash": source_hash,
        "source_file": source_file,
        "source_file_hash": source_file_hash,
        "version": getattr(value, "version", None),
    }
    if bound is not None:
        identity["bound"] = bound
    return identity


def _package_version() -> str:
    try:
        return metadata.version("optiprofiler-evolve")
    except metadata.PackageNotFoundError:
        return "0.1.0a0+source"


def _image_identity(image: str | None) -> Mapping[str, object] | None:
    if image is None:
        return None
    identity: str | None = None
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    else:
        if completed.returncode == 0:
            identity = completed.stdout.strip() or None
    return {"reference": image, "digest": identity}


__all__: list[str] = []
