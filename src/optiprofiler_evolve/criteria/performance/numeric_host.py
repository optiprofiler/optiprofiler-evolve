"""Host-owned Docker lifecycle for numeric solver calls, not agent scheduling.

The scorer requests a call, never an image, command, mount, or host path. Each
call gets a fresh no-network container. Docker authority stays on this host.
"""

from __future__ import annotations

import math
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from optiprofiler_evolve.config import EvaluationConfig
from optiprofiler_evolve.solver import InterfaceSpec

from .numeric_runtime import read_frame, write_frame


class NumericEvaluationSession:
    def __init__(self, candidate: Path, interface: InterfaceSpec, config: EvaluationConfig,
                 runtime: Path) -> None:
        self.candidate, self.interface, self.config, self.runtime = candidate, interface, config, runtime
        self.prefix = f"ope-solver-{uuid.uuid4().hex[:12]}"
        self.calls: dict[str, subprocess.Popen] = {}
        self.names: set[str] = set()
        self.closed: set[str] = set()
        self.threads: list[threading.Thread] = []
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.slots = threading.Semaphore(max(1, math.ceil(config.cpus)))
        self.stopping = threading.Event()
        self.protocol_failed = threading.Event()
        self.records: list[dict] = []
        self.logs = bytearray()
        self.cleanup_records: list[dict] = []

    def _append(self, content: bytes) -> None:
        with self.lock:
            if len(self.logs) < 2 * 1024 * 1024:
                self.logs.extend(content[:2 * 1024 * 1024 - len(self.logs)])

    def _drain(self, pipe) -> None:
        while block := pipe.read(4096):
            self._append(block)

    def _send(self, channel: str, frame: dict) -> None:
        with self.write_lock:
            if not self.stopping.is_set():
                try:
                    write_frame(self.scorer.stdin, {"channel": channel, "frame": frame})
                except (OSError, ValueError):
                    self.stopping.set()

    def _command(self, name: str) -> list[str]:
        return [
            "docker", "run", "--rm", "--pull", "never", "-i", "--init", "--name", name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
            "--pids-limit", str(self.config.pids_limit),
            "--cpus", str(min(1.0, self.config.cpus)), "--memory", self.config.memory,
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m", "--workdir", "/candidate",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1",
            "--mount", f"type=bind,src={self.candidate},dst=/candidate,readonly",
            "--mount", f"type=bind,src={self.runtime},dst=/numeric_runtime.py,readonly",
            "--entrypoint", "python", self.config.solver_image,
            "-I", "/numeric_runtime.py", self.interface.file, self.interface.function,
        ]

    def _call(self, channel: str, args: list) -> None:
        name = f"{self.prefix}-{channel[:12]}"
        record = {"channel": channel, "outcome": "disconnected", "oracle_calls": 0}
        with self.slots:
            with self.lock:
                if self.stopping.is_set() or channel in self.closed:
                    return
                self.names.add(name)
                process = subprocess.Popen(self._command(name), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.calls[channel] = process
            stderr = threading.Thread(target=self._drain, args=(process.stderr,), daemon=True)
            stderr.start()
            try:
                write_frame(process.stdin, {"args": args})
                while True:
                    frame = read_frame(process.stdout)
                    if frame.get("kind") == "oracle":
                        record["oracle_calls"] += 1
                    self._send(channel, frame)
                    if frame.get("kind") in ("return", "error"):
                        record["outcome"] = "returned" if frame["kind"] == "return" else "solver_error"
                        break
            except (EOFError, OSError, ValueError):
                self.protocol_failed.set()
                self._send(channel, {"kind": "error", "error": "Isolated solver disconnected."})
            finally:
                self._remove(name)
                try:
                    process.stdin.close()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                stderr.join(timeout=10)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
                with self.lock:
                    self.records.append(record)
                    self.calls.pop(channel, None)
                    self.names.discard(name)

    def _remove(self, name: str) -> None:
        receipt = remove_numeric_container(name)
        with self.lock:
            self.cleanup_records.append(receipt)
        if receipt["status"] != "removed":
            self._append(f"\n[controller] {receipt}\n".encode())

    def _stop(self) -> None:
        self.stopping.set()
        with self.lock:
            names = list(self.names)
        # Stop the scorer first so no further calls can be requested.
        self._remove(self.scorer_name)
        for name in names:
            self._remove(name)
        if self.scorer.poll() is None:
            self.scorer.kill()

    def run(self, command: list[str], scorer_name: str) -> subprocess.CompletedProcess:
        self.scorer_name = scorer_name
        self.scorer = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        stderr = threading.Thread(target=self._drain, args=(self.scorer.stderr,), daemon=True)
        stderr.start()
        timed_out = threading.Event()

        def timeout():
            timed_out.set()
            self._stop()

        timer = threading.Timer(self.config.timeout_seconds, timeout)
        timer.start()
        protocol_error = False
        try:
            while True:
                try:
                    message = read_frame(self.scorer.stdout)
                except EOFError:
                    break
                channel = message.get("channel")
                if not isinstance(channel, str) or len(channel) != 32 or not channel.isalnum():
                    raise ValueError("Invalid scorer channel.")
                kind = message.get("kind")
                if kind == "start":
                    args = message.get("args")
                    if not isinstance(args, list) or len(args) not in (2, 4, 8, 10):
                        raise ValueError("Invalid solver signature in numeric request.")
                    thread = threading.Thread(target=self._call, args=(channel, args), daemon=True)
                    self.threads.append(thread)
                    thread.start()
                elif kind == "reply":
                    with self.lock:
                        process = self.calls.get(channel)
                    if process is not None:
                        try:
                            write_frame(process.stdin, message["frame"])
                        except (OSError, ValueError):
                            self._send(channel, {"kind": "error"})
                elif kind == "close":
                    with self.lock:
                        self.closed.add(channel)
                        process = self.calls.get(channel)
                    if process is not None and process.poll() is None:
                        self._remove(f"{self.prefix}-{channel[:12]}")
                else:
                    raise ValueError("Invalid scorer event.")
            self.scorer.wait(timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            protocol_error = True
            self._append(f"\n[controller] numeric protocol failure: {exc}\n".encode())
        finally:
            timer.cancel()
            self._stop()
            self.scorer.wait(timeout=10)
            for thread in self.threads:
                thread.join(timeout=30)
            stderr.join(timeout=10)
            for pipe in (self.scorer.stdin, self.scorer.stdout, self.scorer.stderr):
                pipe.close()
        code = (124 if timed_out.is_set() else 125
                if protocol_error or self.protocol_failed.is_set() or not self.records
                or any(item["status"] != "removed" for item in self.cleanup_records)
                else self.scorer.returncode)
        if timed_out.is_set():
            self._append(b"\n[controller] evaluator timed out\n")
        return subprocess.CompletedProcess(command, code, self.logs.decode(errors="replace"))


def remove_numeric_container(name: str) -> dict:
    """Bound daemon work; inability to confirm removal is not a confirmed leak."""
    errors = []
    try:
        result = subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                                text=True, timeout=20, check=False)
        if result.returncode == 0 or "no such container" in result.stderr.lower():
            return {"container": name, "status": "removed", "errors": errors}
        errors.append(result.stderr.strip()[:300])
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(type(exc).__name__)
    # Docker --rm and another cleanup owner can already be removing it. A
    # transient removal conflict is not failure, but absence must be verified.
    for _ in range(10):
        try:
            result = subprocess.run(["docker", "inspect", name], capture_output=True,
                                    text=True, timeout=2, check=False)
            if result.returncode != 0 and any(
                    message in result.stderr.lower() for message in ("no such object", "no such container")):
                return {"container": name, "status": "removed", "errors": errors}
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(type(exc).__name__)
            break
        time.sleep(0.1)
    return {"container": name, "status": "cleanup_unconfirmed", "errors": errors}
