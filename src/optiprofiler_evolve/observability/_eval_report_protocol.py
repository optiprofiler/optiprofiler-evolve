"""Pure schema and cross-field rules for private EvalReport evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from optiprofiler_evolve.observability.schemas import SCHEMA_DIGESTS


class EvalReportError(RuntimeError):
    """The report is missing, malformed or inconsistent."""


class UnsupportedEvalReport(EvalReportError):
    """The report declares a protocol version this reader does not support."""


def validate_schema(value: Mapping[str, Any], schema_name: str) -> None:
    _require_version(value, schema_name)
    schema_resource = files("optiprofiler_evolve.observability.schemas").joinpath(schema_name)
    try:
        raw = schema_resource.read_bytes()
    except OSError as exc:
        raise EvalReportError(f"Could not read bundled {schema_name}: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != SCHEMA_DIGESTS[schema_name]:
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


def _require_version(value: Mapping[str, Any], schema_name: str) -> None:
    expected = "optiprofiler." + schema_name.removesuffix(".schema.json") + "/1"
    declared = value.get("schema")
    if not isinstance(declared, str):
        raise EvalReportError("Report must declare a schema version.")
    if declared != expected:
        raise UnsupportedEvalReport(f"Unsupported schema {declared!r}; expected {expected!r}.")


def companion_receipt(main: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate only the fields needed to withhold private report files."""
    _require_version(main, "eval_report.schema.json")
    receipt = main.get("plot_data")
    if not isinstance(receipt, Mapping) or "path" not in receipt:
        raise EvalReportError("Evaluation report has no numeric companion receipt path.")
    return receipt


def validate_cross_references(
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
    _validate_coverage(main["coverage"])
    identities = set()

    for problem_id, problem in problems.items():
        if problem["library"] is not None and problem["name"] is not None:
            identity = (problem["library"], problem["name"], problem["role"])
            if identity in identities:
                raise EvalReportError("Duplicate problem identity under different IDs.")
            identities.add(identity)
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
            if plot is None:
                raise EvalReportError(f"Problem references unknown plot {plot_ref!r}.")
            if plot.get("problem_id") != problem_id or plot["kind"] != "history":
                raise EvalReportError(f"Plot {plot_ref!r} is not a history plot for problem {problem_id!r}.")

    for plot_ref in main["profiles"]["plot_refs"]:
        if plot_ref not in plots:
            raise EvalReportError(f"Profile references unknown plot {plot_ref!r}.")
        if plots[plot_ref]["kind"] == "history":
            raise EvalReportError(f"Profile references history plot {plot_ref!r}.")
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


def _validate_coverage(coverage: Mapping[str, Any]) -> None:
    if coverage["completed"] > coverage["loaded"]:
        raise EvalReportError("Completed coverage exceeds loaded coverage.")
    selected = coverage["selected"]
    # Archive coverage describes retained observations, not the original selection cohort.
    if coverage["scope"] == "live_selection" and coverage["original_selection_known"] and selected is not None:
        if coverage["loaded"] + (coverage["load_failed"] or 0) > selected:
            raise EvalReportError("Loaded/failed coverage exceeds selected coverage.")


def _validate_history(history: Mapping[str, Any], evaluations: int | None) -> None:
    for channel in history["channels"].values():
        count = channel["count"]
        total = channel["total_evaluations"]
        if total is not None and (count > total or (evaluations is not None and total != evaluations)):
            raise EvalReportError("History count disagrees with observed evaluations.")
        representation = channel["representation"]
        if channel["status"] == "available" and representation is None:
            raise EvalReportError("Available history must declare its representation.")
        if representation == "exact_samples":
            if len(channel["values"]) != count:
                raise EvalReportError("History sample count does not match its values.")
        elif representation == "bin_extrema":
            bins = channel["bins"]
            # v1 fixes bin k to [k*n//32+1, (k+1)*n//32]; all 32 are needed to cover n>64.
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


def _validate_target_work(
    work: Mapping[str, Any], problems: Mapping[str, Mapping[str, Any]], scores: Mapping[str, Any],
) -> None:
    axes = work["axis_values"]
    for name, values in axes.items():
        if len(values) != len(set(values)):
            raise EvalReportError(f"Duplicate target-work {name} axis entry.")
    for identity in axes["problem"]:
        if identity not in problems or problems[identity]["role"] != "primary":
            raise EvalReportError("Target-work problem axis must reference primary problems.")
    if any(index > len(scores["solver_names"]) for index in axes["solver"]):
        raise EvalReportError("Target-work solver axis references an unknown solver.")
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


def _validate_plot(
    plot: Mapping[str, Any], problems: Mapping[str, Mapping[str, Any]],
    scores: Mapping[str, Any], work: Mapping[str, Any] | None,
) -> None:
    solver_count = len(scores["solver_names"])
    if plot["kind"] == "history":
        if plot["problem_id"] not in problems:
            raise EvalReportError("History plot references an unknown problem.")
    else:
        # Non-history plots require target_work_ref in the schema; the caller resolved it.
        if plot["tolerance_index"] != work["tolerance_index"]:
            raise EvalReportError("Plot tolerance does not match its target work.")
    solver_indices = []
    if plot["kind"] == "log_ratio" and solver_count != 2:
        raise EvalReportError("Log-ratio plot requires exactly two solvers.")
    for series in plot["series"]:
        if plot["kind"] == "log_ratio":
            keys = ("x", "y", "visible", "opacity")
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
