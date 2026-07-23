"""Run-level indexing and coverage for controller-owned agent traces."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .config import SandboxConfig, WorkerConfig, WorkersConfig
from .docker_runtime import recover_gateway_trace
from .traces import (
    TracePaths,
    prepare_trace,
    record_workspace_evidence,
    recover_incomplete_traces,
    trace_paths,
    trace_roots,
)


class TraceLedger:
    """Controller-owned run index for all coding-agent invocations."""

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str,
        config_hash: str,
        adapter_name: str,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.run_id = run_id
        self.config_hash = config_hash
        self.adapter_name = adapter_name
        self.index_path = self.run_dir / "controller" / "trace_index.jsonl"
        self.coverage_path = self.run_dir / "controller" / "trace_coverage.json"
        self.public_coverage_path = self.run_dir / "public_trace_coverage.json"
        self._lock = threading.Lock()
        self._seen = self._load_seen_trace_ids()

    def prepare(
        self,
        *,
        root: Path,
        prompt: str,
        command: Sequence[str],
        worker: WorkerConfig,
        workers: WorkersConfig,
        sandbox: SandboxConfig,
        workspace: Path,
        context: Mapping[str, object] | None = None,
        secret_values: Mapping[str, str] | None = None,
    ) -> TracePaths:
        """Prepare one invocation under the run's mandatory trace contract."""

        return prepare_trace(
            root=root,
            prompt=prompt,
            command=command,
            worker=worker,
            workers=workers,
            sandbox=sandbox,
            context=context,
            secret_values=secret_values,
            run_id=self.run_id,
            config_hash=self.config_hash,
            adapter_name=self.adapter_name,
            workspace=workspace,
        )

    def record(self, paths: TracePaths, *, workspace: Path) -> dict[str, object]:
        """Record terminal workspace evidence and append one idempotent index row."""

        record_workspace_evidence(paths, workspace)
        recover_gateway_trace(paths.root)
        entry = _trace_index_entry(self.run_dir, paths)
        self._append_entry(entry)
        return entry

    def recover_and_summarize(self) -> dict[str, object]:
        """Recover interrupted traces, reconcile the index, and write coverage."""

        recover_incomplete_traces(self.run_dir)
        for root in trace_roots(self.run_dir):
            recover_gateway_trace(root)
            paths = trace_paths(root)
            if paths.outcome.is_file():
                self._append_entry(_trace_index_entry(self.run_dir, paths))
        entries = _read_trace_index(self.index_path)
        coverage = _trace_coverage(self.run_id, entries)
        _write_private_json_atomic(self.coverage_path, coverage)
        public = {
            "schema": "public_trace_coverage/1",
            "total": coverage["total"],
            **{
                f"capture_{key}": value
                for key, value in coverage["capture_quality"].items()
            },
            **{
                f"outcome_{key}": value
                for key, value in coverage["outcomes"].items()
            },
            **{
                f"gateway_{key}": value
                for key, value in coverage["provider_gateway"].items()
            },
        }
        _write_json_atomic(self.public_coverage_path, public)
        return coverage

    def _append_entry(self, entry: Mapping[str, object]) -> None:
        trace_id = entry.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("Trace index entries require a nonempty trace_id.")
        if entry.get("run_id") != self.run_id:
            raise ValueError("Trace index entry belongs to a different run.")
        if entry.get("config_hash") != self.config_hash:
            raise ValueError("Trace index entry has a different config hash.")
        with self._lock:
            if trace_id in self._seen:
                return
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            _set_private_mode(self.index_path.parent, 0o700)
            encoded = (json.dumps(entry, sort_keys=True, default=str) + "\n").encode(
                "utf-8"
            )
            descriptor = os.open(
                self.index_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    raise OSError(
                        f"short trace-index write: expected {len(encoded)}, wrote {written}"
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _set_private_mode(self.index_path, 0o600)
            self._seen.add(trace_id)

    def _load_seen_trace_ids(self) -> set[str]:
        _repair_torn_jsonl(self.index_path)
        entries = _read_trace_index(self.index_path)
        for entry in entries:
            if entry.get("run_id") != self.run_id:
                raise ValueError("Existing trace index belongs to a different run.")
            if entry.get("config_hash") != self.config_hash:
                raise ValueError("Existing trace index has a different config hash.")
        return {str(entry["trace_id"]) for entry in entries}


def reconcile_trace_run(run_dir: Path) -> dict[str, object]:
    """Rebuild trace terminal state and coverage after a controller crash."""

    run_dir = run_dir.expanduser().resolve()
    provenance = _read_json_object(run_dir / "provenance.json")
    run_id = provenance.get("run_id")
    config_hash = provenance.get("config_hash")
    components = provenance.get("components")
    worker = components.get("worker") if isinstance(components, Mapping) else None
    adapter = worker.get("adapter") if isinstance(worker, Mapping) else None
    if not all(isinstance(value, str) and value for value in (run_id, config_hash, adapter)):
        raise ValueError("Run provenance lacks trace-ledger identity fields.")
    return TraceLedger(
        run_dir,
        run_id=run_id,
        config_hash=config_hash,
        adapter_name=adapter,
    ).recover_and_summarize()


def _trace_index_entry(run_dir: Path, paths: TracePaths) -> dict[str, object]:
    invocation = _read_json_object(paths.invocation)
    outcome = _read_json_object(paths.outcome)
    workspace = (
        _read_json_object(paths.workspace_evidence)
        if paths.workspace_evidence.is_file()
        else {}
    )
    try:
        relative_path = paths.root.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"Trace directory is outside the run: {paths.root}") from exc
    returncode = outcome.get("returncode")
    state = str(outcome.get("state", "unknown"))
    if state == "interrupted":
        result = "interrupted"
    elif bool(outcome.get("cancelled")):
        result = "cancelled"
    elif bool(outcome.get("timed_out")):
        result = "timed_out"
    elif isinstance(returncode, int) and returncode != 0:
        result = "failed"
    else:
        result = "completed"
    if state == "interrupted" or bool(outcome.get("truncated")):
        quality = "interrupted"
    elif bool(outcome.get("complete")):
        quality = "complete"
    else:
        quality = "degraded"
    join = invocation.get("join", {})
    if not isinstance(join, Mapping):
        join = {}
    streams = outcome.get("streams", {})
    if not isinstance(streams, Mapping):
        streams = {}
    gateway = _gateway_index_payload(paths.root)
    entry = {
        "schema": "trace_index_entry/1",
        "trace_id": invocation.get("trace_id"),
        "run_id": invocation.get("run_id"),
        "relative_path": relative_path,
        "adapter": invocation.get("adapter"),
        "harness": invocation.get("harness"),
        "model": invocation.get("model"),
        "config_hash": invocation.get("config_hash"),
        "join": dict(join),
        "input_tree_hash": invocation.get("input_tree_hash"),
        "output_tree_hash": workspace.get("output_tree_hash"),
        "output_tree_state": workspace.get("output_tree_state"),
        "started_at": invocation.get("started_at"),
        "finished_at": outcome.get("finished_at"),
        "duration_seconds": outcome.get("duration_seconds"),
        "capture_quality": quality,
        "outcome": result,
        "returncode": returncode,
        "termination_reason": outcome.get("termination_reason"),
        "streams": {
            name: {
                "bytes": value.get("bytes"),
                "sha256": value.get("sha256"),
            }
            for name, value in streams.items()
            if name in {"stdout", "stderr", "chunks"} and isinstance(value, Mapping)
        },
    }
    if gateway is not None:
        entry["provider_gateway"] = gateway
    return entry


def _read_trace_index(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(),
        start=1,
    ):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid trace index line {line_number}: {path}") from exc
        if not isinstance(entry, dict) or not isinstance(entry.get("trace_id"), str):
            raise ValueError(f"Invalid trace index entry at line {line_number}: {path}")
        trace_id = entry["trace_id"]
        if trace_id in seen:
            raise ValueError(f"Duplicate trace id {trace_id!r} in {path}")
        seen.add(trace_id)
        entries.append(entry)
    return entries


def _repair_torn_jsonl(path: Path) -> None:
    if not path.is_file():
        return
    content = path.read_bytes()
    if not content or content.endswith(b"\n"):
        return
    boundary = content.rfind(b"\n")
    repaired = content[: boundary + 1] if boundary >= 0 else b""
    descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        if repaired:
            written = os.write(descriptor, repaired)
            if written != len(repaired):
                raise OSError(
                    f"short trace-index repair: expected {len(repaired)}, wrote {written}"
                )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _set_private_mode(path, 0o600)


def _trace_coverage(
    run_id: str,
    entries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    capture = {key: 0 for key in ("complete", "degraded", "interrupted")}
    outcomes = {
        key: 0
        for key in ("completed", "failed", "timed_out", "cancelled", "interrupted")
    }
    degraded_ids: list[str] = []
    interrupted_ids: list[str] = []
    gateways = {key: 0 for key in ("total", "completed", "failed", "interrupted")}
    for entry in entries:
        quality = str(entry.get("capture_quality", "degraded"))
        result = str(entry.get("outcome", "failed"))
        if quality not in capture or result not in outcomes:
            raise ValueError("Trace index contains an unsupported coverage value.")
        capture[quality] += 1
        outcomes[result] += 1
        trace_id = str(entry.get("trace_id", ""))
        if quality == "degraded":
            degraded_ids.append(trace_id)
        elif quality == "interrupted":
            interrupted_ids.append(trace_id)
        gateway = entry.get("provider_gateway")
        if isinstance(gateway, Mapping):
            gateways["total"] += 1
            gateway_outcome = str(gateway.get("outcome", "failed"))
            if gateway_outcome == "completed":
                gateways["completed"] += 1
            elif gateway_outcome == "interrupted":
                gateways["interrupted"] += 1
            else:
                gateways["failed"] += 1
    return {
        "schema": "trace_coverage/1",
        "run_id": run_id,
        "total": len(entries),
        "capture_quality": capture,
        "outcomes": outcomes,
        "provider_gateway": gateways,
        "degraded_trace_ids": degraded_ids,
        "interrupted_trace_ids": interrupted_ids,
    }


def _gateway_index_payload(trace_root: Path) -> dict[str, object] | None:
    path = trace_root / "provider_gateway" / "manifest.json"
    if not path.is_file():
        return None
    payload = _read_json_object(path)
    if payload.get("schema") != "provider_gateway_manifest/1":
        raise ValueError(f"Invalid provider gateway manifest: {path}")
    return {
        "outcome": payload.get("outcome"),
        "request_count": payload.get("request_count"),
        "inflight_request_count": payload.get("inflight_request_count"),
        "audit_failure": payload.get("audit_failure"),
        "exit_code": payload.get("exit_code"),
        "upstream_hash": payload.get("upstream_hash"),
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _write_private_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _set_private_mode(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _set_private_mode(path, 0o600)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _set_private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


__all__: list[str] = []
