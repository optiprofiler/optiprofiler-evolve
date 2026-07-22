"""Crash-tolerant raw capture for one coding-agent invocation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

from .config import SandboxConfig, WorkerConfig, WorkersConfig, plain_data


_CHUNK_BYTES = 64 * 1024
_FSYNC_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class TracePaths:
    """Controller-private files belonging to one agent invocation."""

    root: Path
    input_dir: Path
    stdout: Path
    stderr: Path
    chunks: Path


@dataclass(frozen=True)
class CapturedProcess:
    """Process outcome plus the raw files that survived the invocation."""

    returncode: int
    timed_out: bool
    paths: TracePaths
    capture_error: str | None = None


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
    """Create immutable private inputs before an agent process starts."""

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
    return paths


def run_captured_process(
    *,
    command: Sequence[str],
    prompt: str,
    paths: TracePaths,
    timeout_seconds: int,
    environment: Mapping[str, str],
    cwd: Path | None,
) -> CapturedProcess:
    """Run one process while incrementally preserving both output streams."""

    stdout_handle = _open_private_binary(paths.stdout)
    stderr_handle = _open_private_binary(paths.stderr)
    chunks_handle = _open_private_text(paths.chunks)
    process: subprocess.Popen[bytes] | None = None
    capture_errors: list[str] = []
    lock = threading.Lock()
    sequence = 0

    def record_error(stream: str, exc: BaseException) -> None:
        with lock:
            capture_errors.append(f"{stream}: {type(exc).__name__}: {exc}")

    def drain(stream: str, pipe: BinaryIO, destination: BinaryIO) -> None:
        nonlocal sequence
        offset = 0
        last_sync = time.monotonic()
        write_failed = False
        try:
            while True:
                chunk = pipe.read(_CHUNK_BYTES)
                if not chunk:
                    break
                timestamp = time.monotonic_ns()
                if not write_failed:
                    try:
                        destination.write(chunk)
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
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            cwd=cwd,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        readers = (
            threading.Thread(
                target=drain,
                args=("stdout", process.stdout, stdout_handle),
                name="ope-trace-stdout",
            ),
            threading.Thread(
                target=drain,
                args=("stderr", process.stderr, stderr_handle),
                name="ope-trace-stderr",
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
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
            returncode = 124
        for reader in readers:
            reader.join()
        try:
            chunks_handle.flush()
            os.fsync(chunks_handle.fileno())
        except OSError as exc:
            record_error("chunks", exc)
        return CapturedProcess(
            returncode=returncode,
            timed_out=timed_out,
            paths=paths,
            capture_error="; ".join(capture_errors) or None,
        )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for handle in (stdout_handle, stderr_handle, chunks_handle):
            handle.close()


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
            for line in paths.chunks.read_text(encoding="utf-8").splitlines():
                chunk = json.loads(line)
                source = handles[chunk["stream"]]
                source.seek(int(chunk["offset"]))
                raw = source.read(int(chunk["length"]))
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


def _set_private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


__all__: list[str] = []
