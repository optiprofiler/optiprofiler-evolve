"""Trusted, picklable oracle proxy and in-container stdio relay.

Benchmark child processes use a local socket; the host only launches fixed
solver containers. The socket never enters a candidate container.
"""

from __future__ import annotations

import io
import os
import select
import socket
import socketserver
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .numeric_runtime import MAX_FRAME, pack, read_frame, unpack, write_frame


@dataclass(frozen=True)
class NumericSolver:
    socket_path: str
    timeout: float

    def __call__(self, *args: Any) -> Any:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            with connection.makefile("rwb") as stream:
                write_frame(stream, {"args": [
                    {"oracle": True} if callable(arg) else pack(arg) for arg in args
                ]})
                oracle_errors: dict[str, Exception] = {}
                while True:
                    message = read_frame(stream)
                    kind = message.get("kind")
                    if kind == "return":
                        return None if message.get("value") is None else unpack(message["value"])
                    if kind == "error":
                        error_id = message.get("oracle_error_id")
                        if isinstance(error_id, str) and error_id in oracle_errors:
                            raise oracle_errors[error_id]
                        raise RuntimeError("Isolated solver failed.")
                    index = message.get("index")
                    if (kind != "oracle" or type(index) is not int
                            or index not in (0, 8, 9) or index >= len(args)
                            or not callable(args[index])):
                        raise RuntimeError("Invalid isolated oracle request.")
                    x = unpack(message["x"])
                    # Only the real callback decides what counts as an evaluation.
                    # Preserve catch-and-return behavior on budget exhaustion.
                    try:
                        value = args[index](x)
                    except Exception as exc:
                        error_id = uuid.uuid4().hex
                        oracle_errors[error_id] = exc
                        write_frame(stream, {"kind": "exception", "type": type(exc).__name__,
                                             "oracle_error_id": error_id})
                    else:
                        write_frame(stream, {"kind": "value", "value": pack(value)})


class ScorerRelay:
    """Multiplex local benchmark calls over the scorer's protected stdio."""

    def __init__(self, timeout: float) -> None:
        self.path = f"/tmp/ope-numeric-{uuid.uuid4().hex}.sock"
        self.timeout = timeout
        self.stopping = threading.Event()
        self.input = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
        self.output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        self.lock = threading.Lock()
        self.connections: dict[str, Any] = {}
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                channel = uuid.uuid4().hex
                self.request.settimeout(timeout)
                try:
                    initial = read_frame(self.rfile)
                    with relay.lock:
                        relay.connections[channel] = self.wfile
                        write_frame(relay.output, {"channel": channel, "kind": "start", **initial})
                    while True:
                        frame = read_frame(self.rfile)
                        relay.send({"channel": channel, "kind": "reply", "frame": frame})
                except (EOFError, OSError, ValueError):
                    pass
                finally:
                    with relay.lock:
                        relay.connections.pop(channel, None)
                        write_frame(relay.output, {"channel": channel, "kind": "close"})

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(self.path, Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.reader_thread = threading.Thread(target=self._read, daemon=True)

    def send(self, value: dict) -> None:
        with self.lock:
            write_frame(self.output, value)

    def _read(self) -> None:
        pending = bytearray()
        try:
            while not self.stopping.is_set():
                if not select.select([self.input], [], [], 0.1)[0]:
                    continue
                block = os.read(self.input.fileno(), 65536)
                if not block:
                    break
                pending.extend(block)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    message = read_frame(io.BytesIO(line + b"\n"))
                    with self.lock:
                        stream = self.connections.get(message.get("channel"))
                        if stream is not None:
                            try:
                                write_frame(stream, message["frame"])
                            except (OSError, ValueError):
                                pass
                if len(pending) > MAX_FRAME:
                    raise ValueError("Oversized scorer response.")
        except (EOFError, OSError, ValueError):
            pass

    def __enter__(self):
        self.server_thread.start()
        self.reader_thread.start()
        return NumericSolver(self.path, self.timeout)

    def __exit__(self, *_):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.reader_thread.join(timeout=1)
        if not self.reader_thread.is_alive():
            self.input.close()
        Path(self.path).unlink(missing_ok=True)
