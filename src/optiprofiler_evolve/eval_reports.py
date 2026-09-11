"""Strict, controller-private reader for OptiProfiler evaluation reports."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


CORE_SCHEMA_REVISION = "bf9d61c1482da6f0ad46fab660270cc8484d9f6f"
_SCHEMA_DIGESTS = {
    "eval_report.schema.json": ("9b8f76fd9b2d3eff323304b758ff61c5c456d35b1d81fcf05d3ab9d64453a8a3"),
    "plot_data.schema.json": ("76f83e1b7922a1479d8770888bd417c86fe5f837a450789891618960582d323c"),
}
_DEFAULT_MAIN_LIMIT = 16 * 1024 * 1024
_DEFAULT_COMPANION_LIMIT = 256 * 1024 * 1024


class EvalReportError(RuntimeError):
    """The requested report pair is absent, untrusted, or internally inconsistent."""


@dataclass(frozen=True)
class EvalReportBundle:
    """A validated main report and its integrity-linked numeric companion."""

    report_path: Path
    companion_path: Path
    main: Mapping[str, Any]
    companion: Mapping[str, Any]
    companion_sha256: str

    @property
    def numerical_success(self) -> bool:
        stages = self.main["stages"]
        return (
            stages["numerical"]["status"] == "completed"
            and stages["scoring"]["status"] == "completed"
            and self.main["scores"] is not None
        )

    def history(self, history_id: str) -> Mapping[str, Any]:
        """Return one validated history record without materializing all histories again."""

        return _record(self.companion["histories"], history_id, "history")

    def plot(self, plot_id: str) -> Mapping[str, Any]:
        """Return one validated prepared-plot record by ID."""

        return _record(self.companion["plots"], plot_id, "plot")

    def target_work(self, target_work_id: str) -> Mapping[str, Any]:
        """Return one exact target-work tensor by ID."""

        return _record(self.companion["target_work"], target_work_id, "target work")


def load_eval_report(
    report_path: Path,
    *,
    max_main_bytes: int = _DEFAULT_MAIN_LIMIT,
    max_companion_bytes: int = _DEFAULT_COMPANION_LIMIT,
) -> EvalReportBundle:
    """Load and validate a finalized OptiProfiler report pair.

    Schema validation is necessary but not sufficient. This reader also checks
    file type, containment, byte count, SHA256, evaluation identity, report
    completion, axis dimensions, and every ID reference that Evolve may follow.
    """

    report_path = Path(report_path).resolve()
    main = _read_json_file(report_path, max_main_bytes, "main report")
    _validate_schema(main, "eval_report.schema.json")
    if main["status"] not in {"completed", "partial"}:
        raise EvalReportError(f"Evaluation report is not finalized: {main['status']!r}.")
    if main["plot_data"] is None:
        raise EvalReportError("Evaluation report has no numeric companion receipt.")
    plot_receipt = main["plot_data"]
    if plot_receipt["status"] != "completed":
        raise EvalReportError(
            f"Evaluation report companion is not verifiably completed: {plot_receipt['status']!r}."
        )
    expected_digest = plot_receipt["sha256"]
    if not isinstance(expected_digest, str):
        raise EvalReportError("Evaluation report companion has no verifiable SHA256.")

    companion_path = _contained_relative_file(
        report_path.parent,
        plot_receipt["path"],
        "numeric companion",
    )
    raw = _read_regular_bytes(companion_path, max_companion_bytes, "numeric companion")
    if len(raw) != plot_receipt["bytes"]:
        raise EvalReportError("Numeric companion byte count does not match its receipt.")
    actual_digest = hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_digest.lower():
        raise EvalReportError("Numeric companion SHA256 does not match its receipt.")
    companion = _decode_json(raw, companion_path, "numeric companion")
    _validate_schema(companion, "plot_data.schema.json")
    if companion["evaluation_id"] != main["evaluation_id"]:
        raise EvalReportError("Main report and companion evaluation_id values differ.")

    _validate_cross_references(main, companion)
    bundle = EvalReportBundle(
        report_path=report_path,
        companion_path=companion_path,
        main=main,
        companion=companion,
        companion_sha256=actual_digest,
    )
    if not bundle.numerical_success:
        numerical = main["stages"]["numerical"]["status"]
        scoring = main["stages"]["scoring"]["status"]
        raise EvalReportError(
            "Evaluation report does not contain completed numerical and scoring stages "
            f"(numerical={numerical!r}, scoring={scoring!r})."
        )
    return bundle


def _read_json_file(path: Path, limit: int, label: str) -> Mapping[str, Any]:
    raw = _read_regular_bytes(path, limit, label)
    return _decode_json(raw, path, label)


def _read_regular_bytes(path: Path, limit: int, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise EvalReportError(f"{label.capitalize()} is missing: {path}.") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise EvalReportError(f"{label.capitalize()} must be a regular file.")
    if metadata.st_size > limit:
        raise EvalReportError(f"{label.capitalize()} exceeds the {limit}-byte size limit.")
    return path.read_bytes()


def _decode_json(raw: bytes, path: Path, label: str) -> Mapping[str, Any]:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-standard JSON constant {token!r}")

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvalReportError(f"Could not parse {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise EvalReportError(f"{label.capitalize()} must contain one JSON object.")
    return value


def _validate_schema(value: Mapping[str, Any], schema_name: str) -> None:
    schema_resource = files("optiprofiler_evolve.schemas").joinpath(schema_name)
    raw = schema_resource.read_bytes()
    if hashlib.sha256(raw).hexdigest() != _SCHEMA_DIGESTS[schema_name]:
        raise EvalReportError(f"Bundled {schema_name} does not match its pinned Core schema.")
    schema = json.loads(raw)
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        raise EvalReportError(
            f"{schema_name} validation failed at {location}: {exc.message}"
        ) from exc


def _contained_relative_file(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise EvalReportError(f"{label.capitalize()} path is unavailable.")
    path = Path(value)
    if path.is_absolute():
        raise EvalReportError(f"{label.capitalize()} path must be relative.")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise EvalReportError(f"{label.capitalize()} path escapes the report directory.") from exc
    return resolved


def _validate_cross_references(
    main: Mapping[str, Any],
    companion: Mapping[str, Any],
) -> None:
    histories = _unique_records(companion["histories"], "history")
    plots = _unique_records(companion["plots"], "plot")
    target_work = _unique_records(companion["target_work"], "target work")
    if len(histories) != main["plot_data"]["history_count"]:
        raise EvalReportError("History count does not match the main report receipt.")
    if len(plots) != main["plot_data"]["plot_count"]:
        raise EvalReportError("Plot count does not match the main report receipt.")

    problems = _unique_records(main["problems"], "problem")
    scores = main["scores"]
    if not isinstance(scores, Mapping):
        raise EvalReportError("Finalized report has no score object.")
    solver_names = scores["solver_names"]
    solver_scores = scores["solver_scores"]
    if len(solver_names) != len(solver_scores) or not solver_names:
        raise EvalReportError("Solver score axes have inconsistent lengths.")
    _validate_profile_score_axes(scores, len(solver_names))

    for problem_id, problem in problems.items():
        for run in problem["runs"]:
            solver_index = run["solver_index"]
            if solver_index > len(solver_names):
                raise EvalReportError("Run solver_index exceeds the solver score axis.")
            history_ref = run["history_ref"]
            if history_ref is None:
                continue
            history = histories.get(history_ref)
            if history is None:
                raise EvalReportError(f"Run references unknown history {history_ref!r}.")
            expected = (problem_id, solver_index, run["run_index"])
            actual = (history["problem_id"], history["solver_index"], history["run_index"])
            if actual != expected:
                raise EvalReportError(f"History {history_ref!r} identity does not match its run.")
        for plot_ref in problem.get("plot_refs", []):
            plot = plots.get(plot_ref)
            if plot is None or plot.get("problem_id") != problem_id:
                raise EvalReportError(f"Problem references inconsistent plot {plot_ref!r}.")

    for plot_ref in main["profiles"]["plot_refs"]:
        if plot_ref not in plots:
            raise EvalReportError(f"Profile references unknown plot {plot_ref!r}.")
    for plot in plots.values():
        work_ref = plot.get("target_work_ref")
        if work_ref is not None and work_ref not in target_work:
            raise EvalReportError(f"Plot references unknown target work {work_ref!r}.")


def _validate_profile_score_axes(scores: Mapping[str, Any], solver_count: int) -> None:
    tensor = scores["profile_scores"]
    if tensor is None or tensor == []:
        return
    tolerance_count = len(scores["axis_values"]["tolerance"])
    profile_count = len(scores["axis_values"]["profile_type"])
    if len(tensor) != solver_count:
        raise EvalReportError("Profile score solver axis does not match solver_names.")
    for solver_values in tensor:
        if len(solver_values) != tolerance_count:
            raise EvalReportError("Profile score tolerance axis has the wrong length.")
        for tolerance_values in solver_values:
            if len(tolerance_values) != 2:
                raise EvalReportError("Profile score history/output axis must have length two.")
            if any(len(values) != profile_count for values in tolerance_values):
                raise EvalReportError("Profile score type axis has the wrong length.")


def _unique_records(values: object, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise EvalReportError(f"{label.capitalize()} records must be a list.")
    records: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise EvalReportError(f"{label.capitalize()} record must be an object.")
        identity = str(value["id"])
        if identity in records:
            raise EvalReportError(f"Duplicate {label} ID: {identity!r}.")
        records[identity] = value
    return records


def _record(values: object, identity: str, label: str) -> Mapping[str, Any]:
    records = _unique_records(values, label)
    try:
        return records[identity]
    except KeyError as exc:
        raise KeyError(f"Unknown {label} ID: {identity!r}") from exc


__all__: list[str] = []
