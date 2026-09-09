"""Stable identities for one frozen evolution experiment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from optiprofiler_evolve.shared.config import EvolveConfig
from optiprofiler_evolve.shared.data import DataPlan
from optiprofiler_evolve.shared.solver import InterfaceSpec, tree_hash


@dataclass(frozen=True)
class ExperimentFingerprints:
    """Independent identities for data, split, configuration, and solver code."""

    universe_fingerprint: str
    split_fingerprint: str
    config_fingerprint: str
    code_fingerprint: str
    experiment_fingerprint: str
    schema_version: str = "experiment_fingerprints/1"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def build_experiment_fingerprints(
    *,
    config: EvolveConfig,
    data: DataPlan,
    source_tree: Path,
    interface: InterfaceSpec,
    runtime: str,
    editable: Sequence[str],
) -> ExperimentFingerprints:
    """Build independent hashes without exposing credentials or hidden names."""

    if not data.universe_fingerprint or not data.split_fingerprint:
        raise ValueError("Resolved data must provide universe and split fingerprints.")
    config_fingerprint = canonical_fingerprint(config.resolved_dict())
    code_fingerprint = canonical_fingerprint(
        {
            "source_tree_hash": tree_hash(source_tree),
            "interface": {
                "file": interface.file,
                "function": interface.function,
            },
            "runtime": runtime,
            "editable": list(editable),
        }
    )
    identities = {
        "universe_fingerprint": data.universe_fingerprint,
        "split_fingerprint": data.split_fingerprint,
        "config_fingerprint": config_fingerprint,
        "code_fingerprint": code_fingerprint,
    }
    return ExperimentFingerprints(
        **identities,
        experiment_fingerprint=canonical_fingerprint(identities),
    )


def canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash a JSON-compatible mapping with stable ordering."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__: list[str] = []
