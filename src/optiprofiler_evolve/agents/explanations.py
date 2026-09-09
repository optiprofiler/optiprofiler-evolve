"""Bounded, human-readable explanations emitted beside solver mutations.

Explanation files are untrusted worker metadata. They are normalized into the
run ledger and removed from the executable solver tree before any audit or
evaluation. A missing explanation is visible in the report but does not change
the numerical admission decision.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

EXPLANATION_SCHEMA = "solver_explanation/1"
WORKER_EXPLANATION = Path(".optiprofiler_evolve/solver_explanation.json")


def capture_solver_explanation(
    workspace: Path,
    destination: Path,
    *,
    attempt_id: str,
) -> Path:
    """Capture one worker explanation and remove it from candidate source."""

    source = workspace / WORKER_EXPLANATION
    try:
        if not source.is_file():
            payload: dict[str, Any] = {
                "schema": EXPLANATION_SCHEMA,
                "attempt_id": attempt_id,
                "status": "missing",
                "error": "Worker did not produce solver_explanation.json.",
            }
        else:
            try:
                raw = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    raise ValueError("explanation must be a JSON object")
                payload = _normalize(raw, attempt_id=attempt_id, workspace=workspace)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                payload = {
                    "schema": EXPLANATION_SCHEMA,
                    "attempt_id": attempt_id,
                    "status": "invalid",
                    "error": f"{type(exc).__name__}: {exc}",
                }
    finally:
        metadata_root = workspace / WORKER_EXPLANATION.parts[0]
        if metadata_root.exists():
            shutil.rmtree(metadata_root)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def explanation_status(path: Path) -> str:
    """Return the normalized status without trusting arbitrary worker fields."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(value, Mapping):
        return "invalid"
    status = str(value.get("status", "invalid"))
    return status if status in {"complete", "missing", "invalid"} else "invalid"


def explanation_memory(path: Path) -> dict[str, object] | None:
    """Return a bounded solver-description projection for later workers."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping) or value.get("status") != "complete":
        return None
    mechanisms = value.get("mechanisms", [])
    pseudocode = value.get("pseudocode", [])
    return {
        "summary": str(value.get("summary", ""))[:1000],
        "mechanisms": [
            str(item.get("name", ""))[:200]
            for item in mechanisms[:12]
            if isinstance(item, Mapping) and item.get("name")
        ]
        if isinstance(mechanisms, list)
        else [],
        "pseudocode": [str(item)[:500] for item in pseudocode[:12]]
        if isinstance(pseudocode, list)
        else [],
    }


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


__all__: list[str] = []
