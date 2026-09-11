"""Project trusted evaluator evidence into worker-visible feedback bundles."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

from optiprofiler_evolve.files import write_json
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.observability.eval_reports import (
    EvalReportError,
    private_eval_report_paths,
)
from optiprofiler_evolve.observability.events import EventEmitter

_FULL_ROOT_FILES = {
    "evaluator.log",
    "fitness_breakdown.json",
    "profile_scores.json",
    "redaction_report.json",
}
_FULL_ARTIFACT_SUFFIXES = {
    ".csv",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".log",
    ".md",
    ".pdf",
    ".png",
    ".svg",
    ".txt",
}


def publish_feedback(
    result: EvaluationResult, destination: Path, *, mode: str,
    emit: EventEmitter | None = None,
) -> EvaluationResult:
    """Publish one public assessment through the same boundary for every agent role."""
    worker_result = replace(
        result, error="Evaluation failed; details retained by controller." if result.error else None,
    )
    warning = project_feedback(result.output_dir, destination, worker_result, mode=mode)
    if warning is not None:
        diagnostic = {"mode": result.mode, "error": warning}
        write_json(result.output_dir / "feedback_warning.json", diagnostic)
        if emit is not None:
            emit("feedback_artifacts_withheld", "failed", diagnostic)
    return replace(worker_result, output_dir=destination)


def project_feedback(
    source: Path,
    destination: Path,
    result: EvaluationResult,
    *,
    mode: str,
) -> str | None:
    """Publish feedback; return a controller-private reason if artifacts were withheld."""

    if mode not in {"score_only", "full_artifacts"}:
        raise ValueError(f"Unknown feedback projection mode: {mode!r}")
    if result.assessment is not None and result.assessment["profile"] not in {"smoke", "public"}:
        raise ValueError("Controller-only assessment cannot enter worker feedback.")
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Evaluator evidence directory is missing: {source}")
    if destination.is_relative_to(source):
        raise ValueError("Feedback destination cannot be inside the private evidence tree.")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Feedback destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    _write_worker_result(destination / "result.json", result)
    _write_feedback(destination / "feedback.md", result, mode)
    complexity = None
    if result.assessment is not None:
        public = {
            "profile": result.assessment["profile"], "total": result.assessment_score,
            "results": {
                name: {key: item[key] for key in ("status", "score", "satisfied")}
                for name, item in result.assessment["results"].items()
            },
        }
        (destination / "assessment.json").write_text(json.dumps(public, indent=2) + "\n")
        metrics = result.assessment["results"].get("complexity", {}).get("metrics", {})
        if metrics:
            complexity = _public_complexity(metrics)
    if complexity is not None:
        publish_complexity(destination, complexity)
    if mode == "score_only":
        return

    try:
        copied = _copy_full_artifacts(source, destination)
    except EvalReportError as exc:
        # The numerical result is independent of optional report evidence.
        _write_feedback(destination / "feedback.md", result, "score_only")
        if complexity is not None:
            publish_complexity(destination, complexity)
        with (destination / "feedback.md").open("a", encoding="utf-8") as handle:
            handle.write("\nAdditional artifacts were withheld; the evaluation result is unchanged.\n")
        return f"{type(exc).__name__}: {exc}"
    if complexity is not None:
        copied.append(destination / "complexity.json")
    _write_artifact_index(destination, copied)


def _public_complexity(metrics: dict[str, Any]) -> dict[str, Any]:
    # Only public counts and candidate locations; never forward backend errors or hashes.
    baseline = metrics["baseline"]
    candidate = metrics["candidate"]
    counts = ("decision_complexity", "logical_lines")
    return {
        **{key: metrics[key] for key in (
            "mode", "status", "max_ratio", "branch_ratio", "size_ratio", "complexity_ratio",
            "score", "complexity_percent", "blocked", "schema", "analyzer",
        )},
        "baseline": {key: baseline[key] for key in counts},
        "candidate": {
            **{key: candidate[key] for key in counts},
            "hotspots": [{key: item[key] for key in ("path", "name", "line", "cc")}
                         for item in candidate["hotspots"]],
        } if candidate is not None else None,
        "error": None,
    }


def publish_complexity(destination: Path, report: dict[str, Any]) -> None:
    (destination / "complexity.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8",
    )
    with (destination / "feedback.md").open("a", encoding="utf-8") as handle:
        handle.write("\n" + _complexity_feedback(report))


def _complexity_feedback(report: dict[str, Any]) -> str:
    percent = report["complexity_percent"]
    relative = f"{percent:g}%" if percent is not None else "unavailable"
    limit = report["max_ratio"]
    lines = [
        f"Relative complexity: {relative} / "
        f"limit {100 * limit:g}% — {report['mode']}: {report['status']}",
    ]
    baseline, candidate = report["baseline"], report["candidate"]
    if candidate is not None:
        lines.extend([
            f"Branches: {candidate['decision_complexity']} / "
            f"initial {baseline['decision_complexity']} "
            f"(limit {limit * baseline['decision_complexity']:g})",
            f"Logical lines: {candidate['logical_lines']} / "
            f"initial {baseline['logical_lines']} "
            f"(limit {limit * max(baseline['logical_lines'], 1):g})",
        ])
        for item in candidate["hotspots"]:
            lines.append(f"- {item['path']}:{item['line']} {item['name']}: CC {item['cc']}")
    if report["status"] == "exceeded":
        lines.append(
            "Simplify duplicated logic and unnecessary special cases. "
            "Do not remove documentation or compress formatting to reduce complexity."
        )
    return "\n".join(lines) + "\n"


def _copy_full_artifacts(source: Path, destination: Path) -> list[Path]:
    private: set[Path] = set()
    paths = sorted([*source.iterdir(), *(source / "benchmark").rglob("*")])
    # A root receipt can name a companion inside benchmark, even though it isn't copied itself.
    for report in (path for path in paths if path.name == "eval_report.json"):
        private.update(private_eval_report_paths(report))
    copied: list[Path] = []
    for path in paths:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if path in private:
            continue
        relative = path.relative_to(source)
        if not _is_allowed_full_artifact(relative):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target, follow_symlinks=False)
        copied.append(target)
    return copied


def _is_allowed_full_artifact(relative: Path) -> bool:
    if len(relative.parts) == 1:
        return relative.name in _FULL_ROOT_FILES
    return (
        relative.parts[0] == "benchmark"
        and relative.suffix.lower() in _FULL_ARTIFACT_SUFFIXES
    )


def _write_worker_result(path: Path, result: EvaluationResult) -> None:
    payload = {
        "mode": result.mode,
        "score": result.score,
        "direction": "maximize",
        "problem_count": result.problem_count,
        "success": result.success,
        "error": result.error,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

def _write_feedback(path: Path, result: EvaluationResult, mode: str) -> None:
    if result.success:
        text = (
            f"# Evaluation: {result.mode}\n\n"
            f"- fitness: `{format(result.score, '.6f') if result.score is not None else 'not evaluated'}`\n"
            "- direction: `maximize`\n"
            f"- problems: `{result.problem_count}`\n"
        )
        if mode == "full_artifacts":
            text += "\nRead `artifact_index.json` to discover the allowed benchmark artifacts.\n"
    else:
        text = f"# Evaluation failed\n\n`{result.error}`\n"
    path.write_text(text, encoding="utf-8")


def _write_artifact_index(destination: Path, copied: list[Path]) -> None:
    entries: list[dict[str, Any]] = []
    for path in copied:
        relative = path.relative_to(destination).as_posix()
        media_type, _encoding = mimetypes.guess_type(path.name)
        entries.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "media_type": media_type or "application/octet-stream",
            }
        )
    (destination / "artifact_index.json").write_text(
        json.dumps({"artifacts": entries}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
