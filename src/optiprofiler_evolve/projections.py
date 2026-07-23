"""Default-deny projections from controller events to shareable run state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .events import STATUSES, read_events


_PUBLIC_SCOPE_KEYS = frozenset(
    {
        "phase",
        "iteration",
        "island",
        "attempt_id",
        "step",
        "step_idx",
        "role",
        "job_id",
        "variant_id",
    }
)

# Every public event kind and data key must be named here. Unknown kinds and
# fields stay controller-private, including errors, artifact paths, model names,
# validation scores, prompts, transcripts, and native traces.
_PUBLIC_DATA_KEYS: Mapping[str, frozenset[str]] = {
    "run_started": frozenset(),
    "run_finished": frozenset({"best_candidate_id"}),
    "phase_started": frozenset(),
    "phase_finished": frozenset(),
    "iteration_started": frozenset(),
    "iteration_finished": frozenset({"stop", "attempt_count"}),
    "policy_started": frozenset(),
    "policy_finished": frozenset({"kill", "migrate", "stop"}),
    "attempt_started": frozenset({"parent_id", "guidance"}),
    "attempt_finished": frozenset(
        {
            "candidate_id",
            "parent_id",
            "guidance",
            "public_score",
            "valid",
            "worker_returncode",
            "worker_timed_out",
            "accepted",
            "verdict",
            "outcome",
            "trace_available",
            "trace_bytes",
            "trace_truncated",
            "trace_crash_inferred",
        }
    ),
    "step_started": frozenset(),
    "step_finished": frozenset({"verdict", "outcome"}),
    "worker_started": frozenset(),
    "worker_finished": frozenset(
        {
            "returncode",
            "timed_out",
            "trace_available",
            "trace_bytes",
            "trace_truncated",
            "trace_crash_inferred",
        }
    ),
    "role_agent_started": frozenset(),
    "role_agent_finished": frozenset(
        {
            "returncode",
            "timed_out",
            "trace_available",
            "trace_bytes",
            "trace_truncated",
            "trace_crash_inferred",
        }
    ),
    "integrity_review_started": frozenset(),
    "integrity_review_finished": frozenset({"gate"}),
    "provider_gateway_started": frozenset(),
    "provider_gateway_finished": frozenset({"outcome", "request_count"}),
    "trace_coverage": frozenset(
        {
            "total",
            "capture_complete",
            "capture_degraded",
            "capture_interrupted",
            "outcome_completed",
            "outcome_failed",
            "outcome_timed_out",
            "outcome_cancelled",
            "outcome_interrupted",
        }
    ),
    "directions_ready": frozenset({"mode", "card_count", "status"}),
    "island_analysis_finished": frozenset(
        {"island", "finalist", "strategy_count", "verified_count"}
    ),
    "variant_materialized": frozenset({"variant_id", "accepted"}),
    "research_evaluation_finished": frozenset({"mode", "candidate_id", "success"}),
    "validation_selection_finished": frozenset(
        {"selected_ids", "new_evaluations", "cumulative_evaluations"}
    ),
    "research_finalist_registered": frozenset(),
}


def project_public_events(source: Path, destination: Path) -> list[dict[str, Any]]:
    """Write the deterministic shareable projection of a private event ledger."""

    projected = []
    for event in read_events(source):
        public = _project_event(event)
        if public is not None:
            projected.append(public)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in projected),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return projected


def _project_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = str(event.get("kind", ""))
    allowed_data = _PUBLIC_DATA_KEYS.get(kind)
    if allowed_data is None:
        return None
    status = event.get("status")
    if status not in STATUSES:
        return None
    scope = event.get("scope")
    data = event.get("data")
    if not isinstance(scope, Mapping) or not isinstance(data, Mapping):
        return None
    return {
        "seq": event.get("seq"),
        "ts": event.get("ts"),
        "kind": kind,
        "scope": {key: scope[key] for key in _PUBLIC_SCOPE_KEYS if key in scope},
        "status": status,
        "data": {
            key: data[key]
            for key in allowed_data
            if key in data and _is_public_value(data[key])
        },
    }


def _is_public_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_public_value(item) for item in value)
    return False


__all__: list[str] = []
