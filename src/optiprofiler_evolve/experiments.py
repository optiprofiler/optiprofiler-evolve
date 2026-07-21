"""Reproducible experiment matrices for component ablations."""

from __future__ import annotations

import dataclasses
import json
import warnings
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .config import EvolveConfig
from .models import EvolveResult


ConfigVariant = Callable[[EvolveConfig], EvolveConfig]
RunFunction = Callable[..., EvolveResult]


def run_matrix(
    base: EvolveConfig,
    variants: Mapping[str, ConfigVariant],
    *,
    seeds: Sequence[int],
    initial: str | Path,
    interface: str = "solver.py:solver",
    editable: Sequence[str] = (".",),
    run_root: str | Path,
    run: RunFunction | None = None,
) -> dict[str, Any]:
    """Run immutable config variants and record exact diffs from the base."""

    if not variants or not seeds:
        raise ValueError("run_matrix requires at least one variant and one seed.")
    if not any(name in {"oneshot", "one_shot", "one-shot"} for name in variants):
        warnings.warn(
            "A one-shot baseline is recommended for evolution ablations.",
            stacklevel=2,
        )
    if run is None:
        from .api import evolve

        run = evolve
    root = Path(run_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base_dict = base.resolved_dict()
    summary: dict[str, Any] = {"base_config": base_dict, "variants": {}}
    summary_path = root / "summary.json"
    _write_summary(summary_path, summary)
    for name, transform in variants.items():
        if not name or "/" in name or ".." in name:
            raise ValueError(f"Invalid variant name: {name!r}")
        variant = transform(base)
        variant.validate()
        variant_dict = variant.resolved_dict()
        runs: list[dict[str, Any]] = []
        variant_summary = {
            "config": variant_dict,
            "diff_vs_base": _config_diff(base_dict, variant_dict),
            "runs": runs,
            "n_ok": 0,
            "n_failed": 0,
            "mean_final_score": None,
        }
        summary["variants"][name] = variant_summary
        _write_summary(summary_path, summary)
        for seed in seeds:
            configured = dataclasses.replace(
                variant,
                evolution=dataclasses.replace(variant.evolution, random_seed=int(seed)),
            )
            destination = root / name / f"seed-{seed}"
            entry: dict[str, Any] = {
                "seed": int(seed),
                "run_dir": str(destination),
                "config": configured.resolved_dict(),
            }
            try:
                result = run(
                    initial=initial,
                    interface=interface,
                    editable=editable,
                    config=configured,
                    run_dir=destination,
                )
            except Exception as exc:
                entry.update(
                    {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                entry.update(
                    {
                        "status": "succeeded",
                        "run_dir": str(result.run_dir),
                        "best_candidate_id": result.best_candidate_id,
                        "public_score": result.public_score,
                        "validation_score": result.validation_score,
                        "final_score": result.final_score,
                    }
                )
            runs.append(entry)
            successful = [item for item in runs if item["status"] == "succeeded"]
            variant_summary["n_ok"] = len(successful)
            variant_summary["n_failed"] = len(runs) - len(successful)
            variant_summary["mean_final_score"] = (
                sum(float(item["final_score"]) for item in successful) / len(successful)
                if successful
                else None
            )
            _write_summary(summary_path, summary)
    return summary


def _config_diff(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left:
                changes.append({"path": child, "before": None, "after": right[key]})
            elif key not in right:
                changes.append({"path": child, "before": left[key], "after": None})
            else:
                changes.extend(_config_diff(left[key], right[key], child))
        return changes
    if left != right:
        return [{"path": path, "before": left, "after": right}]
    return []


def _write_summary(path: Path, summary: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__: list[str] = []
