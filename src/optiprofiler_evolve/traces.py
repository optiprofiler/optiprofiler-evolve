"""Crash-tolerant raw capture for one coding-agent invocation."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

from .config import SandboxConfig, WorkerConfig, WorkersConfig, plain_data


_CHUNK_BYTES = 64 * 1024
_FSYNC_INTERVAL_SECONDS = 5.0
_TERMINATE_GRACE_SECONDS = 2.0
_READER_JOIN_SECONDS = 2.0


@dataclass(frozen=True)
class TracePaths:
    """Controller-private files belonging to one agent invocation."""

    root: Path
    input_dir: Path
    stdout: Path
    stderr: Path
    chunks: Path
    invocation: Path
    outcome: Path


@dataclass(frozen=True)
class CapturedProcess:
    """Process outcome plus the raw files that survived the invocation."""

    returncode: int
    timed_out: bool
    paths: TracePaths
    capture_error: str | None = None
    cancelled: bool = False
    termination_reason: str = "exit"
    truncated: bool = False


def prepare_trace(
    *,
    root: Path,
    prompt: str,
    command: Sequence[str],
    worker: WorkerConfig,
    workers: WorkersConfig,
    sandbox: SandboxConfig,
    context: Mapping[str, object] | None = None,
    secret_values: Mapping[str, str] | None = None,
) -> TracePaths:
    """Create private controller inputs before an agent adapter starts."""

    root.parent.mkdir(parents=True, exist_ok=True)
    _harden_trace_parents(root.parent)
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    _set_private_mode(root, 0o700)
    input_dir = root / "input"
    input_dir.mkdir(mode=0o700)
    _set_private_mode(input_dir, 0o700)
    paths = TracePaths(
        root=root,
        input_dir=input_dir,
        stdout=root / "raw.stdout.stream",
        stderr=root / "raw.stderr.stream",
        chunks=root / "chunks.jsonl",
        invocation=root / "invocation.json",
        outcome=root / "outcome.json",
    )
    _write_private_text(input_dir / "prompt.txt", prompt)
    _write_private_json(
        input_dir / "resolved_worker.json",
        {
            "worker": _redacted_worker(worker),
            "workers": {
                "timeout_seconds": workers.timeout_seconds,
                "token_budget": workers.token_budget,
                "max_budget_usd": workers.max_budget_usd,
                "tools": plain_data(workers.tools),
                "adapter": workers.adapter,
            },
            "sandbox": plain_data(sandbox),
        },
    )
    _write_private_json(
        input_dir / "argv.sanitized.json",
        {"argv": _sanitized_argv(command, secret_values or {})},
    )
    payload = dict(context or {})
    payload["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    _write_private_json(input_dir / "input_artifacts.json", payload)
    _write_invocation(paths, worker)
    return paths


def trace_paths(root: Path) -> TracePaths:
    """Return the canonical paths for an already prepared invocation."""

    return TracePaths(
        root=root,
        input_dir=root / "input",
        stdout=root / "raw.stdout.stream",
        stderr=root / "raw.stderr.stream",
        chunks=root / "chunks.jsonl",
        invocation=root / "invocation.json",
        outcome=root / "outcome.json",
    )


def record_sanitized_command(
    paths: TracePaths,
    command: Sequence[str],
    secret_values: Mapping[str, str],
) -> None:
    """Replace the adapter placeholder with the actual command before launch."""

    _write_private_json_atomic(
        paths.input_dir / "argv.sanitized.json",
        {"argv": _sanitized_argv(command, secret_values)},
    )
    invocation = _read_json_object(paths.invocation)
    invocation["input_files"] = _input_evidence(paths.input_dir)
    _write_private_json_atomic(paths.invocation, invocation)


def run_captured_process(
    *,
    command: Sequence[str],
    prompt: str,
    paths: TracePaths,
    timeout_seconds: int,
    environment: Mapping[str, str],
    cwd: Path | None,
    cancellation_event: threading.Event | None = None,
) -> CapturedProcess:
    """Run one process while incrementally preserving both output streams."""

    stdout_handle = _open_private_binary(paths.stdout)
    stderr_handle = _open_private_binary(paths.stderr)
    chunks_handle = _open_private_text(paths.chunks)
    process: subprocess.Popen[bytes] | None = None
    capture_errors: list[str] = []
    lock = threading.Lock()
    sequence = 0
    last_chunks_sync = time.monotonic()
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    truncated = False

    def record_error(stream: str, exc: BaseException) -> None:
        with lock:
            capture_errors.append(f"{stream}: {type(exc).__name__}: {exc}")

    def drain(stream: str, pipe: BinaryIO, destination: BinaryIO) -> None:
        nonlocal last_chunks_sync, sequence
        offset = 0
        last_sync = time.monotonic()
        write_failed = False
        try:
            while True:
                chunk = os.read(pipe.fileno(), _CHUNK_BYTES)
                if not chunk:
                    break
                timestamp = time.monotonic_ns()
                if not write_failed:
                    try:
                        written = destination.write(chunk)
                        if written != len(chunk):
                            raise OSError(
                                f"short trace write: expected {len(chunk)}, wrote {written}"
                            )
                        destination.flush()
                    except OSError as exc:
                        write_failed = True
                        record_error(stream, exc)
                    else:
                        with lock:
                            sequence += 1
                            try:
                                chunks_handle.write(
                                    json.dumps(
                                        {
                                            "stream": stream,
                                            "seq": sequence,
                                            "monotonic_ns": timestamp,
                                            "offset": offset,
                                            "length": len(chunk),
                                        },
                                        sort_keys=True,
                                    )
                                    + "\n"
                                )
                                chunks_handle.flush()
                                now = time.monotonic()
                                if now - last_chunks_sync >= _FSYNC_INTERVAL_SECONDS:
                                    os.fsync(chunks_handle.fileno())
                                    last_chunks_sync = now
                            except OSError as exc:
                                capture_errors.append(
                                    f"chunks: {type(exc).__name__}: {exc}"
                                )
                        offset += len(chunk)
                        now = time.monotonic()
                        if now - last_sync >= _FSYNC_INTERVAL_SECONDS:
                            try:
                                os.fsync(destination.fileno())
                            except OSError as exc:
                                record_error(stream, exc)
                            last_sync = now
        except BaseException as exc:
            record_error(stream, exc)
        finally:
            pipe.close()
            try:
                destination.flush()
                os.fsync(destination.fileno())
            except OSError as exc:
                record_error(stream, exc)

    try:
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(environment),
                cwd=cwd,
                start_new_session=os.name == "posix",
            )
        except BaseException as exc:
            capture_errors.append(f"launch: {type(exc).__name__}: {exc}")
            _sync_handles((stdout_handle, stderr_handle, chunks_handle), capture_errors)
            finalize_trace(
                paths,
                state="launch_failed",
                returncode=None,
                timed_out=False,
                cancelled=False,
                termination_reason="launch_failed",
                started_at=started_at,
                duration_seconds=time.monotonic() - started_monotonic,
                capture_error="; ".join(capture_errors),
                truncated=False,
            )
            raise
        assert process.stdout is not None
        assert process.stderr is not None
        readers = (
            threading.Thread(
                target=drain,
                args=("stdout", process.stdout, stdout_handle),
                name="ope-trace-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=("stderr", process.stderr, stderr_handle),
                name="ope-trace-stderr",
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        if process.stdin is not None:
            try:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
        timed_out = False
        cancelled = False
        termination_reason = "exit"
        deadline = started_monotonic + timeout_seconds
        while process.poll() is None:
            if cancellation_event is not None and cancellation_event.is_set():
                cancelled = True
                termination_reason = "controller_cancelled"
                _terminate_process_group(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                termination_reason = "timeout"
                _terminate_process_group(process)
                break
            try:
                process.wait(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue

        if process.poll() is None:
            _terminate_process_group(process)
        returncode = process.wait()
        if timed_out:
            returncode = 124
        elif cancelled:
            returncode = 130

        for reader, pipe in zip(
            readers, (process.stdout, process.stderr), strict=True
        ):
            reader.join(timeout=_READER_JOIN_SECONDS)
            if reader.is_alive():
                truncated = True
                record_error(reader.name, RuntimeError("reader did not reach EOF"))
                pipe.close()
                reader.join(timeout=_READER_JOIN_SECONDS)
        if any(reader.is_alive() for reader in readers):
            truncated = True
            capture_errors.append("reader: output stream remained open after forced close")

        _sync_handles((stdout_handle, stderr_handle, chunks_handle), capture_errors)
        result = CapturedProcess(
            returncode=returncode,
            timed_out=timed_out,
            paths=paths,
            capture_error="; ".join(capture_errors) or None,
            cancelled=cancelled,
            termination_reason=termination_reason,
            truncated=truncated or bool(capture_errors),
        )
        finalize_trace(
            paths,
            state=(
                "cancelled"
                if cancelled
                else "timed_out"
                if timed_out
                else "completed"
            ),
            returncode=returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            termination_reason=termination_reason,
            started_at=started_at,
            duration_seconds=time.monotonic() - started_monotonic,
            capture_error=result.capture_error,
            truncated=result.truncated,
        )
        return result
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
        for handle in (stdout_handle, stderr_handle, chunks_handle):
            try:
                handle.close()
            except OSError:
                pass


def finalize_adapter_trace(
    paths: TracePaths,
    *,
    transcript: Path,
    native_trace: Path | None,
    stderr_trace: Path | None,
    returncode: int,
    timed_out: bool,
    cancelled: bool,
    capture_error: str | None,
) -> TracePaths:
    """Import adapter evidence into the controller-owned trace directory."""

    if paths.outcome.exists():
        return paths
    degraded: list[str] = [capture_error] if capture_error else []
    sources = (
        (paths.stdout, native_trace or transcript, "stdout"),
        (paths.stderr, stderr_trace, "stderr"),
    )
    for destination, source, stream in sources:
        if destination.exists():
            continue
        if source is not None and source.is_file():
            _copy_private_file(source, destination)
        else:
            _open_private_binary(destination).close()
            if stream == "stdout":
                degraded.append("adapter returned no native stdout; empty fallback recorded")
    if not paths.chunks.exists():
        offset_rows: list[dict[str, object]] = []
        sequence = 0
        for stream, source in (("stdout", paths.stdout), ("stderr", paths.stderr)):
            size = source.stat().st_size
            if size:
                sequence += 1
                offset_rows.append(
                    {
                        "stream": stream,
                        "seq": sequence,
                        "monotonic_ns": None,
                        "offset": 0,
                        "length": size,
                        "synthetic": True,
                    }
                )
        with _open_private_text(paths.chunks) as handle:
            for row in offset_rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        degraded.append("adapter trace imported without native chunk timing")
    invocation = _read_json_object(paths.invocation)
    finalize_trace(
        paths,
        state=(
            "cancelled"
            if cancelled
            else "timed_out"
            if timed_out
            else "failed"
            if returncode != 0 and capture_error
            else "completed"
        ),
        returncode=returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        termination_reason=(
            "controller_cancelled" if cancelled else "timeout" if timed_out else "adapter_exit"
        ),
        started_at=str(invocation.get("started_at", _utc_now())),
        duration_seconds=None,
        capture_error="; ".join(item for item in degraded if item) or None,
        truncated=False,
    )
    return paths


def finalize_trace(
    paths: TracePaths,
    *,
    state: str,
    returncode: int | None,
    timed_out: bool,
    cancelled: bool,
    termination_reason: str,
    started_at: str,
    duration_seconds: float | None,
    capture_error: str | None,
    truncated: bool,
) -> None:
    """Write the terminal manifest once all currently available bytes are durable."""

    payload = {
        "schema": "trace_outcome/1",
        "state": state,
        "returncode": returncode,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "termination_reason": termination_reason,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_seconds": duration_seconds,
        "capture_error": capture_error,
        "derived_error": None,
        "truncated": truncated,
        "complete": capture_error is None and not truncated,
        "streams": {
            "stdout": _file_evidence(paths.stdout),
            "stderr": _file_evidence(paths.stderr),
            "chunks": _file_evidence(paths.chunks),
        },
    }
    _write_private_json_atomic(paths.outcome, payload)


def record_derived_trace_error(paths: TracePaths, error: str) -> None:
    """Record a derived-artifact failure without changing raw completeness."""

    if not paths.outcome.is_file():
        return
    payload = _read_json_object(paths.outcome)
    payload["derived_error"] = error
    _write_private_json_atomic(paths.outcome, payload)


def recover_incomplete_traces(run_dir: Path) -> tuple[Path, ...]:
    """Idempotently mark invocations left without an outcome manifest."""

    recovered: list[Path] = []
    patterns = ("traces/*/invocation.json", "research/traces/*/*/invocation.json")
    for pattern in patterns:
        for invocation_path in sorted(run_dir.glob(pattern)):
            paths = trace_paths(invocation_path.parent)
            if paths.outcome.exists():
                continue
            for path, binary in (
                (paths.stdout, True),
                (paths.stderr, True),
                (paths.chunks, False),
            ):
                if path.exists():
                    continue
                handle = _open_private_binary(path) if binary else _open_private_text(path)
                handle.close()
            invocation = _read_json_object(paths.invocation)
            finalize_trace(
                paths,
                state="interrupted",
                returncode=None,
                timed_out=False,
                cancelled=True,
                termination_reason="controller_interrupted_before_outcome",
                started_at=str(invocation.get("started_at", _utc_now())),
                duration_seconds=None,
                capture_error="controller exited before writing a terminal trace outcome",
                truncated=True,
            )
            recovered.append(paths.root)
    return tuple(recovered)


def render_transcript(paths: TracePaths, destination: Path) -> None:
    """Derive a readable ordered transcript without changing either raw stream."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    handles = {
        "stdout": paths.stdout.open("rb"),
        "stderr": paths.stderr.open("rb"),
    }
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="replace") as output:
            for line_number, line in enumerate(
                paths.chunks.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                try:
                    chunk = json.loads(line)
                    source = handles[str(chunk["stream"])]
                    source.seek(int(chunk["offset"]))
                    raw = source.read(int(chunk["length"]))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    output.write(f"\n[controller] incomplete chunk index at line {line_number}\n")
                    break
                output.write(raw.decode("utf-8", errors="replace"))
        temporary.replace(destination)
        _set_private_mode(destination, 0o600)
    finally:
        for handle in handles.values():
            handle.close()
        temporary.unlink(missing_ok=True)


def _redacted_worker(worker: WorkerConfig) -> dict[str, Any]:
    value = plain_data(worker)
    value["env"] = {
        key: "<redacted>" if _is_secret_name(key) else item
        for key, item in value["env"].items()
    }
    return value


def _sanitized_argv(command: Sequence[str], secret_values: Mapping[str, str]) -> list[str]:
    secrets = {
        value
        for key, value in secret_values.items()
        if value and _is_secret_name(key)
    }
    sanitized: list[str] = []
    redact_next = False
    for argument in command:
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        lowered = argument.lower()
        if argument in secrets:
            sanitized.append("<redacted>")
            continue
        if "=" in argument and _is_secret_name(argument.split("=", 1)[0]):
            sanitized.append(argument.split("=", 1)[0] + "=<redacted>")
            continue
        sanitized.append(argument)
        if argument.startswith("-") and any(
            marker in lowered for marker in ("key", "token", "secret", "password", "auth")
        ):
            redact_next = True
    return sanitized


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(
        marker in upper
        for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    )


def _open_private_binary(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def _open_private_text(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _write_private_text(path: Path, content: str) -> None:
    with _open_private_text(path) as handle:
        handle.write(content)


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_private_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _write_private_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    _write_private_json(temporary, payload)
    temporary.replace(path)
    _set_private_mode(path, 0o600)


def _write_invocation(paths: TracePaths, worker: WorkerConfig) -> None:
    _write_private_json(
        paths.invocation,
        {
            "schema": "trace_invocation/1",
            "state": "started",
            "started_at": _utc_now(),
            "controller_pid": os.getpid(),
            "harness": worker.harness,
            "model": worker.model,
            "input_files": _input_evidence(paths.input_dir),
        },
    )


def _input_evidence(directory: Path) -> dict[str, object]:
    return {
        path.name: _file_evidence(path)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _file_evidence(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"exists": False, "bytes": 0, "sha256": None}
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return {"exists": True, "bytes": size, "sha256": digest.hexdigest()}


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _copy_private_file(source: Path, destination: Path) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle, length=_CHUNK_BYTES)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def _sync_handles(handles: Sequence[BinaryIO], errors: list[str]) -> None:
    for handle in handles:
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            errors.append(f"sync: {type(exc).__name__}: {exc}")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    process.wait()


def _harden_trace_parents(directory: Path) -> None:
    current = directory
    while True:
        _set_private_mode(current, 0o700)
        if current.name == "traces" or current.parent == current:
            return
        current = current.parent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


__all__: list[str] = []
