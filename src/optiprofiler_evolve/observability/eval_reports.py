"""Strict, controller-private reader for OptiProfiler evaluation reports."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from optiprofiler_evolve.files import read_regular_bytes
from optiprofiler_evolve.observability._eval_report_protocol import (
    EvalReportError,
    companion_receipt,
    validate_cross_references,
    validate_schema,
)
from optiprofiler_evolve.observability._eval_report_protocol import (
    UnsupportedEvalReport as UnsupportedEvalReport,
)

_DEFAULT_MAIN_LIMIT = 16 * 1024 * 1024
_DEFAULT_COMPANION_LIMIT = 256 * 1024 * 1024


@dataclass(frozen=True)
class EvalReportBundle:
    """Validated private evidence. Queries borrow records and raise KeyError for unknown IDs."""

    report_path: Path
    companion_path: Path
    main: Mapping[str, Any]
    companion: Mapping[str, Any]

    @cached_property
    def _records(self) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
        return {name: {record["id"]: record for record in self.companion[name]}
                for name in ("histories", "plots", "target_work")}

    def history(self, history_id: str) -> Mapping[str, Any]:
        """Return a borrowed history record; callers must not mutate it."""

        return _record(self._records["histories"], history_id, "history")

    def plot(self, plot_id: str) -> Mapping[str, Any]:
        """Return a borrowed prepared-plot record; callers must not mutate it."""

        return _record(self._records["plots"], plot_id, "plot")

    def target_work(self, target_work_id: str) -> Mapping[str, Any]:
        """Return a borrowed target-work record; callers must not mutate it."""

        return _record(self._records["target_work"], target_work_id, "target work")


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
    validate_schema(main, "eval_report.schema.json")
    plot_receipt = companion_receipt(main)
    if main["status"] not in {"completed", "partial"}:
        raise EvalReportError(f"Evaluation report is not finalized: {main['status']!r}.")
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
    validate_schema(companion, "plot_data.schema.json")
    if companion["evaluation_id"] != main["evaluation_id"]:
        raise EvalReportError("Main report and companion evaluation_id values differ.")

    validate_cross_references(main, companion)
    numerical = main["stages"]["numerical"]["status"]
    scoring = main["stages"]["scoring"]["status"]
    if numerical != "completed" or scoring != "completed" or main["scores"] is None:
        raise EvalReportError(
            "Evaluation report does not contain completed numerical and scoring stages "
            f"(numerical={numerical!r}, scoring={scoring!r})."
        )
    return EvalReportBundle(report_path, companion_path, main, companion)


def _read_json_file(path: Path, limit: int, label: str) -> Mapping[str, Any]:
    # Unlike files.read_json_object, external reports require bounded, strict JSON reads.
    raw = _read_regular_bytes(path, limit, label)
    return _decode_json(raw, path, label)


def _read_regular_bytes(path: Path, limit: int, label: str) -> bytes:
    try:
        return read_regular_bytes(path, limit)
    except FileNotFoundError as exc:
        raise EvalReportError(f"{label.capitalize()} is missing: {path}.") from exc
    except (OSError, ValueError) as exc:
        raise EvalReportError(f"Could not read {label} {path}: {exc}") from exc


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
    receipt = companion_receipt(main)
    companion = _contained_relative_file(report_path.parent, receipt["path"], "numeric companion")
    return frozenset((report_path, companion))


def _record(values: Mapping[str, Mapping[str, Any]], identity: str, label: str) -> Mapping[str, Any]:
    try:
        return values[identity]
    except KeyError:
        raise KeyError(f"Unknown {label} ID: {identity!r}") from None
