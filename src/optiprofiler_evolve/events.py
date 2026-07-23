"""Append-only event ledger and deterministic state reconstruction."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUSES = frozenset({"pending", "running", "succeeded", "failed", "skipped", "cancelled"})

_PUBLIC_TRACE_COVERAGE_KEYS = (
    "total",
    "capture_complete",
    "capture_degraded",
    "capture_interrupted",
    "outcome_completed",
    "outcome_failed",
    "outcome_timed_out",
    "outcome_cancelled",
    "outcome_interrupted",
    "gateway_total",
    "gateway_completed",
    "gateway_failed",
    "gateway_interrupted",
)


@dataclass(frozen=True)
class _EventRequest:
    kind: str
    status: str
    scope: Mapping[str, object]
    data: Mapping[str, object]


class EventWriter:
    """Serialize all concurrent event requests through one writer thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[_EventRequest | None] = queue.Queue()
        self._failure: BaseException | None = None
        self._closed = False
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._write_loop, name="ope-event-writer")
        self._thread.start()

    def emit(
        self,
        kind: str,
        status: str,
        *,
        scope: Mapping[str, object] | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unsupported event status: {status!r}")
        with self._state_lock:
            self._raise_if_failed_locked()
            if self._closed:
                raise RuntimeError("Event writer is closed.")
            self._queue.put(_EventRequest(kind, status, dict(scope or {}), dict(data or {})))

    def flush(self) -> None:
        self._queue.join()
        self._raise_if_failed()

    def close(self) -> None:
        with self._state_lock:
            if not self._closed:
                self._closed = True
                if self._thread.is_alive():
                    self._queue.put(None)
        self._queue.join()
        self._thread.join()
        self._raise_if_failed()

    def _write_loop(self) -> None:
        sequence = 0
        try:
            handle = self.path.open("a", encoding="utf-8")
        except BaseException as exc:
            self._record_failure(exc)
            handle = None
        try:
            while True:
                request = self._queue.get()
                try:
                    if request is None:
                        return
                    if handle is None or self._has_failed():
                        continue
                    sequence += 1
                    event = {
                        "seq": sequence,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "kind": request.kind,
                        "scope": dict(request.scope),
                        "status": request.status,
                        "data": dict(request.data),
                    }
                    try:
                        handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
                        handle.flush()
                    except BaseException as exc:
                        self._record_failure(exc)
                finally:
                    self._queue.task_done()
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException as exc:
                    self._record_failure(exc)

    def _record_failure(self, failure: BaseException) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = failure

    def _has_failed(self) -> bool:
        with self._state_lock:
            return self._failure is not None

    def _raise_if_failed(self) -> None:
        with self._state_lock:
            self._raise_if_failed_locked()

    def _raise_if_failed_locked(self) -> None:
        failure = self._failure
        if failure is not None:
            raise RuntimeError("Event writer failed.") from failure


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read complete events, tolerating one torn final line after a crash."""

    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        events.append(event)
    return events


def rebuild_run_state(path: Path) -> dict[str, Any]:
    """Build the versioned, shareable run state from a public event ledger."""

    events = read_events(path)
    run: dict[str, Any] = {
        "status": "pending",
        "started": None,
        "finished": None,
        "best_candidate_id": None,
    }
    phases: dict[str, dict[str, Any]] = {}
    phase_order: dict[str, int] = {}
    iterations: dict[int, dict[str, Any]] = {}
    policies: dict[int, dict[tuple[int | None, str], dict[str, Any]]] = {}
    attempts: dict[str, dict[str, Any]] = {}
    roles: dict[str, dict[str, Any]] = {}
    trace_coverage: dict[str, Any] | None = None

    for position, event in enumerate(events):
        scope = event.get("scope", {})
        data = event.get("data", {})
        kind = str(event.get("kind", ""))
        status = event.get("status")
        if (
            status not in STATUSES
            or not isinstance(scope, Mapping)
            or not isinstance(data, Mapping)
        ):
            continue
        boundary = _event_boundary(event)

        if kind == "run_started":
            _start_lifecycle(run, status, boundary)
        elif kind == "run_finished":
            _finish_lifecycle(run, status, boundary)
            candidate_id = data.get("best_candidate_id")
            if isinstance(candidate_id, str):
                run["best_candidate_id"] = candidate_id

        phase_name = scope.get("phase")
        if phase_name is not None and kind in {"phase_started", "phase_finished"}:
            name = str(phase_name)
            phase = phases.setdefault(name, _lifecycle_record(name=name))
            phase_order.setdefault(name, position)
            if kind == "phase_started":
                _start_lifecycle(phase, status, boundary)
            else:
                _finish_lifecycle(phase, status, boundary)

        iteration_value = _optional_int(scope.get("iteration"))
        if iteration_value is not None and kind in {
            "iteration_started",
            "iteration_finished",
        }:
            iteration = iterations.setdefault(
                iteration_value,
                _lifecycle_record(iteration=iteration_value, policies=[]),
            )
            if kind == "iteration_started":
                _start_lifecycle(iteration, status, boundary)
            else:
                _finish_lifecycle(iteration, status, boundary)
                if isinstance(data.get("stop"), bool):
                    iteration["stop"] = data["stop"]
                attempt_count = _optional_int(data.get("attempt_count"))
                if attempt_count is not None:
                    iteration["attempt_count"] = attempt_count

        if iteration_value is not None and kind in {"policy_started", "policy_finished"}:
            name = str(scope.get("step", "policy"))
            index = _optional_int(scope.get("step_idx"))
            policy = policies.setdefault(iteration_value, {}).setdefault(
                (index, name),
                _lifecycle_record(name=name, index=index),
            )
            if kind == "policy_started":
                _start_lifecycle(policy, status, boundary)
            else:
                _finish_lifecycle(policy, status, boundary)
                policy["kill_count"] = _sequence_size(data.get("kill"))
                policy["migration_count"] = _sequence_size(data.get("migrate"))
                if isinstance(data.get("stop"), bool):
                    policy["stop"] = data["stop"]

        attempt_id_value = scope.get("attempt_id")
        if attempt_id_value is not None:
            attempt_id = str(attempt_id_value)
            attempt = attempts.setdefault(
                attempt_id,
                _new_attempt(attempt_id, scope),
            )
            _update_attempt_scope(attempt, scope)
            if kind == "attempt_started":
                _start_lifecycle(attempt, status, boundary)
                _copy_scalars(attempt, data, ("parent_id", "guidance"))
            elif kind == "attempt_finished":
                _finish_lifecycle(attempt, status, boundary)
                _copy_scalars(
                    attempt,
                    data,
                    (
                        "candidate_id",
                        "parent_id",
                        "guidance",
                        "public_score",
                        "valid",
                        "accepted",
                        "verdict",
                        "outcome",
                    ),
                )
            elif kind in {"step_started", "step_finished"}:
                _update_attempt_step(attempt, scope, data, kind, status, boundary)
            elif kind in {"worker_started", "worker_finished"}:
                _update_nested_lifecycle(
                    attempt,
                    "worker",
                    kind,
                    status,
                    boundary,
                    data,
                    (
                        "returncode",
                        "timed_out",
                        "trace_available",
                        "trace_bytes",
                        "trace_truncated",
                        "trace_crash_inferred",
                    ),
                )
            elif kind in {"integrity_review_started", "integrity_review_finished"}:
                _update_nested_lifecycle(
                    attempt,
                    "integrity_review",
                    kind,
                    status,
                    boundary,
                    data,
                    ("gate",),
                )
            elif kind in {"provider_gateway_started", "provider_gateway_finished"}:
                _update_nested_lifecycle(
                    attempt,
                    "provider_gateway",
                    kind,
                    status,
                    boundary,
                    data,
                    ("outcome", "request_count"),
                )

        job_id_value = scope.get("job_id")
        role_value = scope.get("role")
        if (
            job_id_value is not None
            and role_value is not None
            and kind in {"role_agent_started", "role_agent_finished"}
        ):
            job_id = str(job_id_value)
            role = roles.setdefault(
                job_id,
                _lifecycle_record(
                    job_id=job_id,
                    role=str(role_value),
                    phase=str(scope.get("phase", "")),
                    island=_optional_int(scope.get("island")),
                ),
            )
            if kind == "role_agent_started":
                _start_lifecycle(role, status, boundary)
            else:
                _finish_lifecycle(role, status, boundary)
                _copy_scalars(
                    role,
                    data,
                    (
                        "returncode",
                        "timed_out",
                        "trace_available",
                        "trace_bytes",
                        "trace_truncated",
                        "trace_crash_inferred",
                    ),
                )

        if kind == "trace_coverage":
            trace_coverage = {"status": status}
            _copy_scalars(trace_coverage, data, _PUBLIC_TRACE_COVERAGE_KEYS)

    phase_list = sorted(phases.values(), key=lambda item: phase_order[item["name"]])
    attempt_list = sorted(
        attempts.values(),
        key=lambda item: (
            item.get("iteration") is None,
            item.get("iteration") or 0,
            item.get("island") is None,
            item.get("island") or 0,
            item["attempt_id"],
        ),
    )
    for attempt in attempt_list:
        attempt["steps"].sort(
            key=lambda item: (
                item.get("index") is None,
                item.get("index") or 0,
                item["name"],
            )
        )
    iteration_list = []
    for iteration_value in sorted(iterations):
        iteration = iterations[iteration_value]
        iteration["policies"] = sorted(
            policies.get(iteration_value, {}).values(),
            key=lambda item: (
                item.get("index") is None,
                item.get("index") or 0,
                item["name"],
            ),
        )
        iteration_list.append(iteration)

    return {
        "schema": "optiprofiler_evolve_public_run_state/1",
        "last_seq": events[-1].get("seq", 0) if events else 0,
        "updated_at": events[-1].get("ts") if events else None,
        "run": run,
        "phases": phase_list,
        "iterations": iteration_list,
        "matrix": _build_island_matrix(attempt_list),
        "attempts": attempt_list,
        "roles": sorted(
            roles.values(),
            key=lambda item: (
                (item.get("started") or {}).get("seq", 0),
                item["job_id"],
            ),
        ),
        "trace_coverage": trace_coverage,
    }


def _event_boundary(event: Mapping[str, Any]) -> dict[str, Any]:
    return {"seq": event.get("seq"), "ts": event.get("ts")}


def _lifecycle_record(**values: object) -> dict[str, Any]:
    return {**values, "status": "pending", "started": None, "finished": None}


def _start_lifecycle(record: dict[str, Any], status: object, boundary: Mapping[str, Any]) -> None:
    record["status"] = status
    if record.get("started") is None:
        record["started"] = dict(boundary)


def _finish_lifecycle(record: dict[str, Any], status: object, boundary: Mapping[str, Any]) -> None:
    record["status"] = status
    record["finished"] = dict(boundary)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sequence_size(value: object) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0


def _copy_scalars(
    destination: dict[str, Any], source: Mapping[str, Any], keys: tuple[str, ...]
) -> None:
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            destination[key] = value


def _new_attempt(attempt_id: str, scope: Mapping[str, Any]) -> dict[str, Any]:
    return _lifecycle_record(
        attempt_id=attempt_id,
        phase=str(scope.get("phase", "")),
        iteration=_optional_int(scope.get("iteration")),
        island=_optional_int(scope.get("island")),
        steps=[],
    )


def _update_attempt_scope(attempt: dict[str, Any], scope: Mapping[str, Any]) -> None:
    if not attempt.get("phase") and scope.get("phase") is not None:
        attempt["phase"] = str(scope["phase"])
    for key in ("iteration", "island"):
        value = _optional_int(scope.get(key))
        if attempt.get(key) is None and value is not None:
            attempt[key] = value


def _update_attempt_step(
    attempt: dict[str, Any],
    scope: Mapping[str, Any],
    data: Mapping[str, Any],
    kind: str,
    status: object,
    boundary: Mapping[str, Any],
) -> None:
    name = str(scope.get("step", "step"))
    index = _optional_int(scope.get("step_idx"))
    step = next(
        (item for item in attempt["steps"] if item["name"] == name and item["index"] == index),
        None,
    )
    if step is None:
        step = _lifecycle_record(name=name, index=index)
        attempt["steps"].append(step)
    if kind == "step_started":
        _start_lifecycle(step, status, boundary)
    else:
        _finish_lifecycle(step, status, boundary)
        _copy_scalars(step, data, ("verdict", "outcome"))


def _update_nested_lifecycle(
    owner: dict[str, Any],
    key: str,
    kind: str,
    status: object,
    boundary: Mapping[str, Any],
    data: Mapping[str, Any],
    public_keys: tuple[str, ...],
) -> None:
    record = owner.setdefault(key, _lifecycle_record())
    if kind.endswith("_started"):
        _start_lifecycle(record, status, boundary)
    else:
        _finish_lifecycle(record, status, boundary)
        _copy_scalars(record, data, public_keys)


def _build_island_matrix(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for attempt in attempts:
        iteration = attempt.get("iteration")
        island = attempt.get("island")
        if isinstance(iteration, int) and isinstance(island, int):
            cells.setdefault((iteration, island), []).append(attempt)

    matrix = []
    for (iteration, island), members in sorted(cells.items()):
        counts = {status: 0 for status in sorted(STATUSES)}
        accepted = 0
        quarantined = 0
        for attempt in members:
            status = str(attempt.get("status", "pending"))
            if status in counts:
                counts[status] += 1
            accepted += int(attempt.get("accepted") is True)
            review = attempt.get("integrity_review")
            if isinstance(review, Mapping) and review.get("gate") in {
                "quarantined",
                "unavailable",
            }:
                quarantined += 1
        matrix.append(
            {
                "iteration": iteration,
                "island": island,
                "status": _aggregate_attempt_status(counts),
                "attempt_ids": [attempt["attempt_id"] for attempt in members],
                "counts": {**counts, "accepted": accepted, "quarantined": quarantined},
            }
        )
    return matrix


def _aggregate_attempt_status(counts: Mapping[str, int]) -> str:
    if counts.get("running", 0) or counts.get("pending", 0):
        return "running"
    if counts.get("succeeded", 0):
        return "succeeded"
    if counts.get("failed", 0):
        return "failed"
    if counts.get("cancelled", 0):
        return "cancelled"
    return "skipped"


def write_run_state(events_path: Path, destination: Path) -> dict[str, Any]:
    """Atomically write the current event projection."""

    state = rebuild_run_state(events_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return state


__all__: list[str] = []
