"""Resolve and freeze problem-library datasets for one evolution run."""

from __future__ import annotations

import hashlib
import importlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import DataConfig


@dataclass(frozen=True)
class DataPlan:
    """Immutable problem names visible at each evaluation boundary."""

    library: str
    selection: dict[str, Any]
    universe: tuple[str, ...]
    public: tuple[str, ...]
    hidden: tuple[str, ...]
    smoke: tuple[str, ...]
    split_seed: int
    manifest_hash: str
    custom_problem_libraries_path: str | None = None

    @property
    def final(self) -> tuple[str, ...]:
        """Public plus hidden problems used by controller-only final ranking."""

        return self.public + self.hidden

    def public_manifest(self) -> dict[str, Any]:
        """Return only information that may be shown to coding workers."""

        return {
            "library": self.library,
            "selection": self.selection,
            "public": list(self.public),
            "smoke": list(self.smoke),
            "manifest_hash": self.manifest_hash,
        }

    def full_manifest(self) -> dict[str, Any]:
        result = asdict(self)
        result["final"] = list(self.final)
        return result


def resolve_data_plan(config: DataConfig) -> DataPlan:
    """Resolve exact problem names and create a deterministic split."""

    universe = tuple(sorted(config.problem_names or _select_problem_names(config)))
    if not universe:
        raise ValueError("The selected problem-library experiment contains no problems.")
    if len(set(universe)) != len(universe):
        raise ValueError("The selected problem universe contains duplicate names.")

    split = config.split
    if split.public:
        public = tuple(split.public)
        hidden = tuple(split.hidden)
        unknown = set(public).union(hidden).difference(universe)
        omitted = set(universe).difference(public).difference(hidden)
        if unknown:
            raise ValueError(f"Explicit split contains unknown problems: {sorted(unknown)!r}")
        if omitted:
            raise ValueError(f"Explicit split omits selected problems: {sorted(omitted)!r}")
    else:
        shuffled = list(universe)
        random.Random(split.seed).shuffle(shuffled)
        if split.hidden_fraction == 0 or len(shuffled) == 1:
            hidden_count = 0
        else:
            hidden_count = max(1, round(len(shuffled) * split.hidden_fraction))
            hidden_count = min(hidden_count, len(shuffled) - 1)
        hidden = tuple(sorted(shuffled[:hidden_count]))
        public = tuple(sorted(shuffled[hidden_count:]))

    if not public:
        raise ValueError("The public split cannot be empty.")
    smoke_count = min(split.smoke_count, len(public))
    smoke_pool = list(public)
    random.Random(split.seed ^ 0x5A17).shuffle(smoke_pool)
    smoke = tuple(sorted(smoke_pool[:smoke_count]))

    payload = {
        "library": config.library,
        "selection": dict(config.selection),
        "universe": list(universe),
        "public": list(public),
        "hidden": list(hidden),
        "smoke": list(smoke),
        "split_seed": split.seed,
        "custom_problem_libraries_path": config.custom_problem_libraries_path,
    }
    manifest_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DataPlan(
        library=config.library,
        selection=dict(config.selection),
        universe=universe,
        public=public,
        hidden=hidden,
        smoke=smoke,
        split_seed=split.seed,
        manifest_hash=manifest_hash,
        custom_problem_libraries_path=config.custom_problem_libraries_path,
    )


def write_data_manifests(plan: DataPlan, run_dir: Path) -> None:
    """Write separate trusted and worker-visible manifests."""

    trusted = run_dir / "controller" / "data_manifest.json"
    public = run_dir / "public_data_manifest.json"
    trusted.parent.mkdir(parents=True, exist_ok=True)
    trusted.write_text(
        json.dumps(plan.full_manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    public.write_text(
        json.dumps(plan.public_manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _select_problem_names(config: DataConfig) -> list[str]:
    try:
        module = importlib.import_module(f"optiprofiler.problem_libs.{config.library}")
    except ImportError as exc:
        raise RuntimeError(
            f"Could not import OptiProfiler problem library {config.library!r}. "
            "Install/configure it or provide data.problem_names explicitly."
        ) from exc
    selector_name = f"{config.library}_select"
    selector = getattr(module, selector_name, None)
    if not callable(selector):
        raise RuntimeError(f"Problem library {config.library!r} does not expose {selector_name}().")
    names = selector(dict(config.selection))
    if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) for name in names):
        raise TypeError(f"{selector_name}() must return a sequence of problem names.")
    return list(names)


__all__: list[str] = []
