"""Project trusted evaluator evidence into worker-visible feedback bundles."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from optiprofiler_evolve.criteria.complexity.analyzer import ComplexityReport
from optiprofiler_evolve.models import EvaluationResult

AgentReportValidator = Callable[[Mapping[str, object]], None]

_FULL_ROOT_FILES = {
    "agent_report.json",
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


def project_feedback(
    source: Path,
    destination: Path,
    result: EvaluationResult,
    *,
    mode: str,
    validate_agent_report: AgentReportValidator | None = None,
    complexity: ComplexityReport | None = None,
) -> None:
    """Publish exactly the evidence permitted by one feedback mode.

    ``structured_numeric`` intentionally knows nothing about the Core report
    schema. The Core-owned validator must be injected by the integration layer;
    without it, publication fails closed.
    """

    if mode not in {"score_only", "structured_numeric", "full_artifacts"}:
        raise ValueError(f"Unknown feedback projection mode: {mode!r}")
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Evaluator evidence directory is missing: {source}")
    if _is_relative_to(destination, source):
        raise ValueError("Feedback destination cannot be inside the private evidence tree.")
    agent_report = None
    if mode == "structured_numeric" and result.score is not None:
        agent_report = _validated_agent_report(source, validate_agent_report)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Feedback destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    _write_worker_result(destination / "result.json", result)
    _write_feedback(destination / "feedback.md", result, mode)
    if result.assessment is not None:
        if result.assessment["profile"] not in {"smoke", "public"}:
            raise ValueError("Controller-only assessment cannot enter worker feedback.")
        public = {
            "profile": result.assessment["profile"], "total": result.assessment_score,
            "results": {
                name: {key: item[key] for key in ("status", "score", "satisfied")}
                for name, item in result.assessment["results"].items()
            },
        }
        (destination / "assessment.json").write_text(json.dumps(public, indent=2) + "\n")
    if complexity is not None:
        publish_complexity(destination, complexity)
    if mode == "score_only":
        return
    if mode == "structured_numeric" and result.score is not None:
        assert agent_report is not None
        _write_agent_report(destination, agent_report)
        return

    copied = _copy_full_artifacts(source, destination)
    if complexity is not None:
        copied.append(destination / "complexity.json")
    _write_artifact_index(destination, copied)


def publish_complexity(destination: Path, report: ComplexityReport) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "complexity.json").write_text(
        json.dumps(report.worker_dict(), indent=2) + "\n", encoding="utf-8",
    )
    with (destination / "feedback.md").open("a", encoding="utf-8") as handle:
        handle.write("\n" + report.feedback())


def _validated_agent_report(
    source: Path,
    validator: AgentReportValidator | None,
) -> Mapping[str, object]:
    if validator is None:
        raise RuntimeError("The formal agent_report/1 contract validator is unavailable.")
    report_path = source / "agent_report.json"
    report = _read_json_mapping(report_path)
    validator(report)
    return report


def _write_agent_report(
    destination: Path,
    report: Mapping[str, object],
) -> None:
    (destination / "agent_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _copy_full_artifacts(source: Path, destination: Path) -> list[Path]:
    copied: list[Path] = []
    for path in sorted(source.rglob("*")):
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
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
        if mode == "structured_numeric":
            text += "\nStructured numeric evidence is available in `agent_report.json`.\n"
        elif mode == "full_artifacts":
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


def _read_json_mapping(path: Path) -> Mapping[str, object]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("agent_report.json must be a regular file.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError("agent_report.json must contain a JSON object.")
    return value


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


__all__: list[str] = []
