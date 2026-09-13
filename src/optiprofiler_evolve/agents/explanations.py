"""Bounded, human-readable explanations emitted beside solver mutations.

Explanation files are untrusted worker metadata. They are captured and validated
here and removed from the executable solver tree before any audit or evaluation.
A missing explanation is visible in the report but does not change the numerical
admission decision.

Projecting a captured explanation into ledger fields, such as its normalized
status or its bounded memory summary, lives in ``observability.attempts``: those
are presentation derivations over the captured file, and keeping them here made
the agent and observability layers mutually dependent.
"""

from __future__ import annotations

import json
import shutil
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from optiprofiler_evolve.files import read_regular_bytes, write_json

EXPLANATION_SCHEMA = "solver_explanation/1"
WORKER_EXPLANATION = Path(".optiprofiler_evolve/solver_explanation.json")
_MAX_EXPLANATION_BYTES = 1024 * 1024


def explanation_instructions(solver_directory: str = ".") -> str:
    """The complete explanation contract for each independent solver-edit job."""
    output = PurePosixPath(solver_directory) / WORKER_EXPLANATION.as_posix()
    return f"""Before finishing, write `{output}` using schema `solver_explanation/1`.
   The JSON object must contain the literal top-level field `"schema": "solver_explanation/1"`.
   Include a concise `summary`; a `pseudocode` list; a `mechanisms` list whose entries
   have `name`, `description`, and `code_bindings`; plus `expected_strengths`,
   `known_limitations`, and `evidence_consulted` string lists. Each code binding has
   `file` (relative to the solver directory) and `lines` (one-based `[start, end]`).
   Count the actual lines in each referenced file before writing ranges; never
   invent end lines. Parse your JSON and check every referenced path/range.
   Describe the algorithm that actually remains in the files, not an intended or
   discarded experiment. Keep the JSON file within 1 MiB."""


def capture_solver_explanation(
    workspace: Path,
    destination: Path,
    *,
    attempt_id: str,
) -> Path:
    """Capture one worker explanation and remove it from candidate source."""

    source = workspace / WORKER_EXPLANATION
    metadata_root = source.parent
    try:
        metadata = metadata_root.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Explanation directory must be a real directory, not a link.")
    try:
        encoded = read_regular_bytes(source, _MAX_EXPLANATION_BYTES)
    except FileNotFoundError:
        payload: dict[str, Any] = {
            "schema": EXPLANATION_SCHEMA, "attempt_id": attempt_id,
            "status": "missing", "error": "Worker did not produce solver_explanation.json.",
        }
    else:
        try:
            raw = json.loads(encoded.decode("utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("explanation must be a JSON object")
            payload = _normalize(raw, attempt_id=attempt_id, workspace=workspace)
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            payload = {
                "schema": EXPLANATION_SCHEMA, "attempt_id": attempt_id,
                "status": "invalid", "error": f"{type(exc).__name__}: {exc}",
            }
    write_json(destination, payload)
    if metadata is not None:
        shutil.rmtree(metadata_root)
    return destination




def _normalize(
    raw: Mapping[str, Any],
    *,
    attempt_id: str,
    workspace: Path,
) -> dict[str, Any]:
    if raw.get("schema") != EXPLANATION_SCHEMA:
        raise ValueError(f"schema must be {EXPLANATION_SCHEMA!r}")
    summary = _text(raw.get("summary"), required=True, maximum=4000)
    pseudocode = _text_list(raw.get("pseudocode"), maximum_items=40, item_limit=1000)
    if not pseudocode:
        raise ValueError("pseudocode must contain at least one step")

    raw_mechanisms = raw.get("mechanisms", [])
    if not isinstance(raw_mechanisms, Sequence) or isinstance(raw_mechanisms, (str, bytes)):
        raise ValueError("mechanisms must be a list")
    mechanisms = []
    for raw_mechanism in raw_mechanisms[:20]:
        if not isinstance(raw_mechanism, Mapping):
            raise ValueError("each mechanism must be an object")
        bindings = []
        raw_bindings = raw_mechanism.get("code_bindings", [])
        if not isinstance(raw_bindings, Sequence) or isinstance(raw_bindings, (str, bytes)):
            raise ValueError("code_bindings must be a list")
        for binding in raw_bindings[:30]:
            if not isinstance(binding, Mapping):
                raise ValueError("each code binding must be an object")
            file_name = _safe_relative(str(binding.get("file", "")))
            lines = binding.get("lines", [])
            if not (
                isinstance(lines, Sequence)
                and not isinstance(lines, (str, bytes))
                and len(lines) == 2
                and all(isinstance(value, int) and value >= 1 for value in lines)
                and int(lines[0]) <= int(lines[1])
            ):
                raise ValueError("code binding lines must be [start, end] positive integers")
            _validate_code_binding(
                workspace,
                file_name,
                start=int(lines[0]),
                end=int(lines[1]),
            )
            bindings.append({"file": file_name, "lines": [int(lines[0]), int(lines[1])]})
        mechanisms.append(
            {
                "name": _text(raw_mechanism.get("name"), required=True, maximum=200),
                "description": _text(
                    raw_mechanism.get("description"), required=True, maximum=2000
                ),
                "code_bindings": bindings,
            }
        )
    if not mechanisms:
        raise ValueError("mechanisms must contain at least one item")

    return {
        "schema": EXPLANATION_SCHEMA,
        "attempt_id": attempt_id,
        "status": "complete",
        "summary": summary,
        "pseudocode": pseudocode,
        "mechanisms": mechanisms,
        "expected_strengths": _text_list(
            raw.get("expected_strengths"), maximum_items=20, item_limit=1000
        ),
        "known_limitations": _text_list(
            raw.get("known_limitations"), maximum_items=20, item_limit=1000
        ),
        "evidence_consulted": _text_list(
            raw.get("evidence_consulted"), maximum_items=40, item_limit=1000
        ),
    }


def _text(value: object, *, required: bool, maximum: int) -> str:
    text = str(value if value is not None else "").strip()
    if required and not text:
        raise ValueError("required explanation text is empty")
    return text[:maximum]


def _text_list(value: object, *, maximum_items: int, item_limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("explanation field must be a list of strings")
    return [
        text
        for item in value[:maximum_items]
        if (text := _text(item, required=False, maximum=item_limit))
    ]


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe code binding path: {value!r}")
    return path.as_posix()


def _validate_code_binding(
    workspace: Path,
    file_name: str,
    *,
    start: int,
    end: int,
) -> None:
    root = workspace.resolve()
    source = (workspace / file_name).resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError(f"code binding does not name a candidate file: {file_name!r}")
    try:
        line_count = len(source.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"code binding file is not readable text: {file_name!r}") from exc
    if end > line_count:
        raise ValueError(
            f"code binding lines [{start}, {end}] exceed {file_name!r} "
            f"with {line_count} lines"
        )
