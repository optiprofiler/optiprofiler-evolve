"""Agent-job identity validation, role input copies and invocation diagnostics.

These helpers serve the agent-invocation path: job/worker identity checks, role
input preparation, worker-exit diagnostics and trace scope labels. They are owned
here (the agent layer) rather than scattered over the caller modules that invoke
agents on behalf of controller, phases and observability.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from optiprofiler_evolve.agents.contracts import WorkerOutcome


def guidance_id(direction: Mapping[str, object] | None) -> str | None:
    return str(direction["card_id"]) if direction and direction.get("card_id") else None


def worker_exit_warning(outcome: WorkerOutcome | None) -> str | None:
    """Describe an abnormal worker exit without judging its workspace."""

    if outcome is None:
        return None
    if outcome.returncode == 0 and not outcome.timed_out and not outcome.cancelled:
        return None
    parts = [f"worker_exit_warning: returncode={outcome.returncode}"]
    if outcome.timed_out:
        parts.append("timed_out")
    if outcome.cancelled:
        parts.append("cancelled")
    if outcome.termination_reason:
        parts.append(f"termination={outcome.termination_reason}")
    return ", ".join(parts)


def safe_identifier(value: str, label: str) -> str:
    if not value or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def copy_role_input(source: Path, destination: Path) -> None:
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(f"Role input does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"Duplicate role input destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise ValueError(f"Role input is not a regular file or directory: {source}")


def trace_event_scope(context: Mapping[str, object]) -> dict[str, object]:
    join = context.get("join")
    if not isinstance(join, Mapping):
        return {}
    allowed = {
        "phase",
        "iteration",
        "island",
        "attempt_id",
        "role",
        "job_id",
    }
    return {key: value for key, value in join.items() if key in allowed}


def redact_role_input(path: Path, replacements: Mapping[str, str]) -> None:
    candidates = [path] if path.is_file() else list(path.rglob("*"))
    text_suffixes = {".json", ".jsonl", ".md", ".txt", ".log", ".diff", ".yaml", ".yml"}
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() not in text_suffixes:
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for before, after in replacements.items():
            text = text.replace(before, after)
        candidate.write_text(text, encoding="utf-8")
