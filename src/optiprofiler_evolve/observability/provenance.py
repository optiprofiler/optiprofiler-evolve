"""Stable experiment identity and best-effort environment capture."""

from __future__ import annotations

import hashlib
import inspect
import json
import platform
import subprocess
import sys
from collections.abc import Mapping
from functools import partial
from importlib import metadata
from pathlib import Path
from typing import Any

from optiprofiler_evolve.config import EvolveConfig


def coordinate_seed(base_seed: int, *coordinates: object) -> int:
    """Derive independent random streams that do not shift under ablations."""

    payload = json.dumps([base_seed, *coordinates], separators=(",", ":"), default=str)
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def build_run_provenance(
    config: EvolveConfig,
    *,
    run_id: str,
    components: Mapping[str, object],
    prompt_templates: Mapping[str, str],
) -> dict[str, Any]:
    """Build the redacted identity written into the first run event."""

    resolved = config.resolved_dict()
    encoded = json.dumps(resolved, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "schema_version": "1",
        "run_id": run_id,
        "config": resolved,
        "config_hash": hashlib.sha256(encoded.encode()).hexdigest(),
        "components": dict(components),
        "environment": {
            "package_version": _package_version(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "worker_image": _image_identity(
                config.sandbox.worker_image if config.sandbox.backend == "docker" else None
            ),
            "evaluator_image": _image_identity(
                config.execution.docker_image
                if config.assessment.enabled("performance") and config.execution.backend == "docker"
                else None
            ),
        },
        "seed_coordinates": ["seed", "phase", "iteration", "island", "attempt", "component"],
        "prompt_templates": dict(prompt_templates),
    }


def callable_identity(value: object) -> dict[str, Any]:
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
