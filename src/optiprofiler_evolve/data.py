"""Resolve and freeze problem-library datasets for one evolution run."""

from __future__ import annotations

import hashlib
import importlib
import json
import random
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import DataConfig


@dataclass(frozen=True)
class DataPlan:
    """Immutable problem names visible at each evaluation boundary."""

    library: str
    selection: dict[str, Any]
    universe: tuple[str, ...]
    public: tuple[str, ...]
    validation: tuple[str, ...]
    hidden: tuple[str, ...]
    smoke: tuple[str, ...]
    split_seed: int
    manifest_hash: str
    custom_problem_libraries_path: str | None = None
    aliases: Mapping[str, str] | None = None

    @property
    def selection_problems(self) -> tuple[str, ...]:
        """Controller-only set used for population selection."""

        return self.validation or self.public

    def opaque_name(self, problem_name: str) -> str:
        """Return the worker-visible identifier for one trusted problem name."""

        if self.aliases is None:
            return problem_name
        return self.aliases[problem_name]

    def public_manifest(self) -> dict[str, Any]:
        """Return only information that may be shown to coding workers."""

        return {
            "experiment": self.manifest_hash[:12],
            "public_problem_count": len(self.public),
            "smoke_problem_count": len(self.smoke),
            "budget_note": "each problem enforces a fixed evaluation budget",
        }

    def full_manifest(self) -> dict[str, Any]:
        return asdict(self)


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
        validation = tuple(split.validation)
        hidden = tuple(split.hidden)
        assigned = set(public).union(validation).union(hidden)
        unknown = assigned.difference(universe)
        omitted = set(universe).difference(assigned)
        if unknown:
            raise ValueError(f"Explicit split contains unknown problems: {sorted(unknown)!r}")
        if omitted:
            raise ValueError(f"Explicit split omits selected problems: {sorted(omitted)!r}")
    else:
        shuffled = list(universe)
        random.Random(split.seed).shuffle(shuffled)
        available = max(0, len(shuffled) - 1)
        validation_count = _split_count(len(shuffled), split.validation_fraction, available)
        available -= validation_count
        hidden_count = _split_count(len(shuffled), split.hidden_fraction, available)
        hidden = tuple(sorted(shuffled[:hidden_count]))
        validation = tuple(sorted(shuffled[hidden_count : hidden_count + validation_count]))
        public = tuple(sorted(shuffled[hidden_count + validation_count :]))

    if not public:
        raise ValueError("The public split cannot be empty.")
    if split.smoke:
        smoke = tuple(split.smoke)
    else:
        smoke_count = min(split.smoke_count, len(public))
        smoke_pool = list(public)
        random.Random(split.seed ^ 0x5A17).shuffle(smoke_pool)
        smoke = tuple(sorted(smoke_pool[:smoke_count]))

    payload = {
        "library": config.library,
        "selection": dict(config.selection),
        "universe": list(universe),
        "public": list(public),
        "validation": list(validation),
        "hidden": list(hidden),
        "smoke": list(smoke),
        "split_seed": split.seed,
        "custom_problem_libraries_path": config.custom_problem_libraries_path,
    }
    manifest_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    aliases = {name: _new_opaque_name() for name in universe}
    return DataPlan(
        library=config.library,
        selection=dict(config.selection),
        universe=universe,
        public=public,
        validation=validation,
        hidden=hidden,
        smoke=smoke,
        split_seed=split.seed,
        manifest_hash=manifest_hash,
        custom_problem_libraries_path=config.custom_problem_libraries_path,
        aliases=aliases,
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
        from optiprofiler.problem_libraries import (
            _resolve_problem_library_options,
            load_problem_library,
            resolve_problem_library,
        )
    except ImportError:
        names = _select_legacy_problem_names(config)
    else:
        try:
            reference = resolve_problem_library(
                config.library,
                config.custom_problem_libraries_path,
            )
            plugin = load_problem_library(reference)
            library_options = _resolve_problem_library_options(plugin, {})
            names = plugin.select(dict(config.selection), library_options)
        except Exception as exc:
            raise RuntimeError(
                f"Could not select problems from OptiProfiler library {config.library!r}."
            ) from exc
    if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) for name in names):
        raise TypeError("A problem-library selector must return a sequence of problem names.")
    return list(names)


def _select_legacy_problem_names(config: DataConfig) -> Any:
    if config.custom_problem_libraries_path is not None:
        raise RuntimeError(
            "Custom and external problem libraries require the current OptiProfiler "
            "problem-library protocol."
        )
    try:
        module = importlib.import_module(f"optiprofiler.problem_libs.{config.library}")
        selector = getattr(module, f"{config.library}_select")
        return selector(dict(config.selection))
    except Exception as exc:
        raise RuntimeError(
            f"Could not select legacy OptiProfiler library {config.library!r}."
        ) from exc


def _split_count(total: int, fraction: float, available: int) -> int:
    if fraction == 0 or total <= 1 or available == 0:
        return 0
    return min(max(1, round(total * fraction)), available)


def _new_opaque_name() -> str:
    return f"P_{secrets.token_hex(8).upper()}"


__all__: list[str] = []
