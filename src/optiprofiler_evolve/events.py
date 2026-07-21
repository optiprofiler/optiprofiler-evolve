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
    """Project phase, iteration, island, attempt, and step state from events."""

    events = read_events(path)
    state: dict[str, Any] = {
        "last_seq": events[-1]["seq"] if events else 0,
        "run": None,
        "phases": {},
        "matrix": {},
        "attempts": {},
        "roles": {},
    }
    for event in events:
        scope = event.get("scope", {})
        kind = str(event.get("kind", ""))
        status = event.get("status")
        if kind.startswith("run_"):
            state["run"] = status
        phase = scope.get("phase")
        if phase is not None and kind in {"phase_started", "phase_finished"}:
            state["phases"][str(phase)] = status
        iteration = scope.get("iteration")
        island = scope.get("island")
        if iteration is not None and island is not None:
            key = f"{iteration}:{island}"
            cell = state["matrix"].setdefault(key, {"status": status, "attempts": []})
            cell["status"] = status
            attempt_id = scope.get("attempt_id")
            if attempt_id is not None and attempt_id not in cell["attempts"]:
                cell["attempts"].append(attempt_id)
        attempt_id = scope.get("attempt_id")
        if attempt_id is not None:
            attempt = state["attempts"].setdefault(str(attempt_id), {"status": status, "steps": []})
            attempt["status"] = status
            step = scope.get("step")
            if step is not None:
                step_index = scope.get("step_idx")
                current = next(
                    (
                        item
                        for item in attempt["steps"]
                        if item["name"] == step and item["index"] == step_index
                    ),
                    None,
                )
                update = {
                    "name": step,
                    "index": step_index,
                    "status": status,
                    "seq": event["seq"],
                }
                if current is None:
                    attempt["steps"].append(update)
                else:
                    current.update(update)
        job_id = scope.get("job_id")
        role = scope.get("role")
        if job_id is not None and role is not None:
            state["roles"][str(job_id)] = {
                "role": str(role),
                "phase": str(scope.get("phase", "")),
                "island": scope.get("island"),
                "status": status,
                "seq": event["seq"],
            }
    return state


def write_run_state(events_path: Path, destination: Path) -> dict[str, Any]:
    """Atomically write the current event projection."""

    state = rebuild_run_state(events_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return state


__all__: list[str] = []
