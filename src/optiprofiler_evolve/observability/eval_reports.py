"""Strict, controller-private reader for OptiProfiler evaluation reports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping
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
    """Validated private evidence; records are borrowed and must not be mutated."""

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
        """Find a history by ID in linear time, without rebuilding an index."""

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

    report_path = _main_path(report_path)
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
    if limit < 0:
        raise EvalReportError(f"{label.capitalize()} size limit must be nonnegative.")
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise EvalReportError(f"{label.capitalize()} must be a regular file, not a symlink.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise EvalReportError(f"{label.capitalize()} must be a regular file.")
            if opened.st_size > limit:
                raise EvalReportError(f"{label.capitalize()} exceeds the {limit}-byte size limit.")
            raw = handle.read(limit + 1)
    except FileNotFoundError as exc:
        raise EvalReportError(f"{label.capitalize()} is missing: {path}.") from exc
    except (OSError, ValueError) as exc:
        raise EvalReportError(f"Could not read {label} {path}: {exc}") from exc
    if len(raw) > limit:
        raise EvalReportError(f"{label.capitalize()} exceeds the {limit}-byte size limit.")
    return raw


def _decode_json(raw: bytes, path: Path, label: str) -> Mapping[str, Any]:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-standard JSON constant {token!r}")

    def finite_float(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number {token!r}")
        return value

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant, parse_float=finite_float)
    except (ValueError, RecursionError) as exc:
        raise EvalReportError(f"Could not parse {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise EvalReportError(f"{label.capitalize()} must contain one JSON object.")
    return value


def _validate_schema(value: Mapping[str, Any], schema_name: str) -> None:
    schema_resource = files("optiprofiler_evolve.observability.schemas").joinpath(schema_name)
    try:
        raw = schema_resource.read_bytes()
    except OSError as exc:
        raise EvalReportError(f"Could not read bundled {schema_name}: {exc}") from exc
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
    except RecursionError as exc:
        raise EvalReportError(f"{schema_name} report nesting exceeds validation limits.") from exc


def _contained_relative_file(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise EvalReportError(f"{label.capitalize()} path is unavailable.")
    path = Path(value)
    if path.is_absolute():
        raise EvalReportError(f"{label.capitalize()} path must be relative.")
    try:
        current = root.resolve()
        for part in path.parts:
            current /= part
            if current.is_symlink():
                raise EvalReportError(f"{label.capitalize()} path must not contain a symlink.")
        resolved = current.resolve()
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise EvalReportError(f"{label.capitalize()} path escapes the report directory.") from exc
    except EvalReportError:
        raise
    except (OSError, RuntimeError) as exc:
        raise EvalReportError(f"Could not resolve {label} path: {exc}") from exc
    return resolved


def _main_path(path: Path) -> Path:
    path = Path(path)
    try:
        # The caller authorizes the parent; preserve the final component for the link check.
        return path.parent.resolve() / path.name
    except (OSError, RuntimeError) as exc:
        raise EvalReportError(f"Could not resolve main report path: {exc}") from exc


def private_eval_report_paths(report_path: Path) -> frozenset[Path]:
    """Read only a main report to identify files excluded from worker feedback.

    This validates the receipt's structure and containment, not its numeric evidence.
    A missing or malformed receipt cannot authorize copying the surrounding JSON files.
    """
    report_path = _main_path(report_path)
    main = _read_json_file(report_path, _DEFAULT_MAIN_LIMIT, "main report")
    _validate_schema(main, "eval_report.schema.json")
    receipt = main["plot_data"]
    if receipt is None:
        raise EvalReportError("Evaluation report has no numeric companion receipt.")
    companion = _contained_relative_file(report_path.parent, receipt["path"], "numeric companion")
    return frozenset((report_path, companion))


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
    _validate_problem_coverage(main)

    for problem_id, problem in problems.items():
        run_keys = [(run["solver_index"], run["run_index"]) for run in problem["runs"]]
        if len(set(run_keys)) != len(run_keys):
            raise EvalReportError(f"Duplicate run identity in problem {problem_id!r}.")
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
            _validate_history(history, run["evaluations"])
        for plot_ref in problem.get("plot_refs", []):
            plot = plots.get(plot_ref)
            if plot is None or plot.get("problem_id") != problem_id or plot["kind"] != "history":
                raise EvalReportError(f"Problem references inconsistent plot {plot_ref!r}.")

    for plot_ref in main["profiles"]["plot_refs"]:
        if plot_ref not in plots or plots[plot_ref]["kind"] == "history":
            raise EvalReportError(f"Profile references unknown plot {plot_ref!r}.")
    referenced = {run["history_ref"] for p in problems.values() for run in p["runs"]}
    if set(histories).difference(referenced):
        raise EvalReportError("Companion contains an unreferenced history.")
    for work in target_work.values():
        _validate_target_work(work, problems, scores)
    for plot in plots.values():
        work_ref = plot.get("target_work_ref")
        if work_ref is not None and work_ref not in target_work:
            raise EvalReportError(f"Plot references unknown target work {work_ref!r}.")
        _validate_plot(plot, problems, scores, target_work.get(work_ref))
        renderer = plot.get("renderer_variants_ref")
        if renderer is not None and renderer not in companion["semantics"].get("renderer_variants", {}):
            raise EvalReportError("Plot references unknown renderer variant metadata.")


def _validate_problem_coverage(main: Mapping[str, Any]) -> None:
    coverage = main["coverage"]
    if coverage["completed"] > coverage["loaded"]:
        raise EvalReportError("Completed coverage exceeds loaded coverage.")
    selected = coverage["selected"]
    if selected is not None:
        if coverage["loaded"] + (coverage["load_failed"] or 0) > selected:
            raise EvalReportError("Loaded/failed coverage exceeds selected coverage.")
    identities = set()
    for problem in main["problems"]:
        if problem["library"] is not None and problem["name"] is not None:
            identity = (problem["library"], problem["name"], problem["role"])
            if identity in identities:
                raise EvalReportError("Duplicate problem identity under different IDs.")
            identities.add(identity)


def _validate_history(history: Mapping[str, Any], evaluations: int | None) -> None:
    for channel in history["channels"].values():
        count = channel["count"]
        total = channel["total_evaluations"]
        if total is not None and (count > total or evaluations is not None and total != evaluations):
            raise EvalReportError("History count disagrees with observed evaluations.")
        representation = channel["representation"]
        if representation == "exact_samples":
            if len(channel["values"]) != count:
                raise EvalReportError("History sample count does not match its values.")
        elif representation == "bin_extrema":
            bins = channel["bins"]
            if len(bins) != 32:
                raise EvalReportError("Binned history must contain the 32 protocol intervals.")
            for index, item in enumerate(bins):
                start, end = index * count // 32 + 1, (index + 1) * count // 32
                if (item["start_index"], item["end_index"]) != (start, end):
                    raise EvalReportError("History bin intervals do not partition its count.")
                nonfinite = sum(item["nonfinite"].values())
                if nonfinite > end - start + 1:
                    raise EvalReportError("History bin nonfinite counts exceed its width.")
                low, high = item["finite_min"], item["finite_max"]
                all_nonfinite = nonfinite == end - start + 1
                if (low is None) != all_nonfinite or (high is None) != all_nonfinite:
                    raise EvalReportError("History bin extrema disagree with nonfinite counts.")
                if low is not None:
                    if low["value"] > high["value"]:
                        raise EvalReportError("History bin minimum exceeds maximum.")
                    if any(not start <= bound["evaluation_index"] <= end for bound in (low, high)):
                        raise EvalReportError("History bin extremum index is outside its interval.")


def _validate_target_work(work, problems, scores) -> None:
    axes = work["axis_values"]
    for name, values in axes.items():
        if len(values) != len(set(values)):
            raise EvalReportError(f"Duplicate target-work {name} axis entry.")
    for identity in axes["problem"]:
        if identity not in problems or problems[identity]["role"] != "primary":
            raise EvalReportError("Target-work problem axis must reference primary problems.")
    if axes["solver"] != list(range(1, len(scores["solver_names"]) + 1)):
        raise EvalReportError("Target-work solver axis does not match solver scores.")
    if axes["run"] != list(range(1, len(axes["run"]) + 1)) or not axes["run"]:
        raise EvalReportError("Target-work run axis must be consecutive and one-based.")
    tolerances = scores["axis_values"]["tolerance"]
    # JSON Schema integers also permit encodings such as 1.0.
    index = int(work["tolerance_index"])
    if index > len(tolerances) or work["tolerance"] != tolerances[index - 1]:
        raise EvalReportError("Target-work tolerance does not match score axes.")
    # The explicit problem axis is authoritative; never reorder tensors by main-report order.
    for kind in ("history", "output"):
        tensor = work[kind]
        if len(tensor) != len(axes["problem"]):
            raise EvalReportError("Target-work problem dimension does not match its axis.")
        for row in tensor:
            if len(row) != len(axes["solver"]) or any(len(v) != len(axes["run"]) for v in row):
                raise EvalReportError("Target-work solver/run dimensions do not match their axes.")
    expected_runs = {(s, r) for s in axes["solver"] for r in axes["run"]}
    for identity in axes["problem"]:
        runs = {(r["solver_index"], r["run_index"]) for r in problems[identity]["runs"]}
        if not expected_runs.issubset(runs):
            raise EvalReportError("Target-work axis references an unknown problem run.")


def _validate_plot(plot, problems, scores, work) -> None:
    solver_count = len(scores["solver_names"])
    if plot["kind"] == "history":
        if plot["problem_id"] not in problems:
            raise EvalReportError("History plot references an unknown problem.")
    else:
        if plot["tolerance_index"] != work["tolerance_index"]:
            raise EvalReportError("Plot tolerance does not match its target work.")
    solver_indices = []
    for series in plot["series"]:
        if plot["kind"] == "log_ratio":
            keys = ("x", "y", "visible", "opacity")
            if solver_count != 2:
                raise EvalReportError("Log-ratio plot requires exactly two solvers.")
        else:
            keys = ("x", "mean", "lower", "upper")
            if plot["kind"] == "history":
                keys += ("evaluation_indices",)
            solver_indices.append(series["solver_index"])
            if series["solver_index"] > solver_count:
                raise EvalReportError("Plot solver_index exceeds the score axis.")
        if len({len(series[key]) for key in keys}) != 1:
            raise EvalReportError("Plot coordinate arrays have inconsistent lengths.")
    if len(solver_indices) != len(set(solver_indices)):
        raise EvalReportError("Plot contains duplicate solver series.")
    if plot["kind"] == "log_ratio":
        count = len(plot["series"][0]["x"])
        for group in plot["bar_groups"]:
            if not 1 <= group["start_index"] <= group["end_index"] <= count:
                raise EvalReportError("Log-ratio bar group is outside its coordinate range.")
        if plot["problem_mapping"] == "bar_sources":
            sources = plot["bar_sources"]
            if any(len(values) != count for values in sources.values()):
                raise EvalReportError("Log-ratio bar-source arrays have inconsistent lengths.")
            if any(i > len(work["axis_values"]["problem"]) for i in sources["problem_index"]):
                raise EvalReportError("Log-ratio bar references an unknown problem index.")
            if any(i not in work["axis_values"]["run"] for i in sources["run_index"]):
                raise EvalReportError("Log-ratio bar references an unknown run index.")
    elif plot["kind"] != "history" and plot["n_runs"] != len(work["axis_values"]["run"]):
        raise EvalReportError("Profile plot run count does not match target work.")


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


def _unique_records(values: list[Mapping[str, Any]], label: str) -> dict[str, Mapping[str, Any]]:
    records: dict[str, Mapping[str, Any]] = {}
    for value in values:
        identity = value["id"]
        if identity in records:
            raise EvalReportError(f"Duplicate {label} ID: {identity!r}.")
        records[identity] = value
    return records


def _record(values: list[Mapping[str, Any]], identity: str, label: str) -> Mapping[str, Any]:
    for value in values:
        if value["id"] == identity:
            return value
    raise KeyError(f"Unknown {label} ID: {identity!r}")


__all__: list[str] = []
