"""Pinned model-provider transport used by isolated coding workers."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import secrets
import signal
import socket
import ssl
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


_PROTOCOL_ROUTES = {
    "anthropic": frozenset(
        {
            ("POST", "/v1/messages"),
            ("POST", "/v1/messages/count_tokens"),
            ("GET", "/v1/models"),
        }
    ),
    "openai_responses": frozenset(
        {
            ("POST", "/v1/responses"),
            ("GET", "/v1/models"),
        }
    ),
}
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_WORKER_STRIPPED_HEADERS = _HOP_BY_HOP_HEADERS | frozenset(
    {
        "authorization",
        "cookie",
        "forwarded",
        "host",
        "proxy-connection",
        "x-api-key",
    }
)
_RESPONSE_STRIPPED_HEADERS = _HOP_BY_HOP_HEADERS | frozenset({"set-cookie"})
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class GatewayRoute:
    """One immutable upstream route and its controller-owned credential."""

    protocol: str
    upstream_base_url: str
    credential: str = field(repr=False)
    auth_mode: str = "bearer"
    max_request_bytes: int = 16_000_000
    connect_timeout_seconds: int = 15
    response_timeout_seconds: int = 900
    allow_private_upstream: bool = False

    def __post_init__(self) -> None:
        if self.protocol not in _PROTOCOL_ROUTES:
            raise ValueError(f"Unsupported gateway protocol: {self.protocol!r}")
        if self.auth_mode not in {"bearer", "x-api-key"}:
            raise ValueError("Gateway auth_mode must be bearer or x-api-key.")
        if not self.credential:
            raise ValueError("Gateway credential cannot be empty.")
        parsed = urlsplit(self.upstream_base_url)
        allowed_schemes = {"https"} | ({"http"} if self.allow_private_upstream else set())
        if parsed.scheme not in allowed_schemes or not parsed.hostname:
            raise ValueError("Gateway upstream must be a pinned https base URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Gateway upstream cannot contain credentials, query, or fragment.")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Gateway upstream contains an invalid port.") from exc
        decoded_path = unquote(parsed.path)
        if "%" in parsed.path or any(
            part in {".", ".."} for part in decoded_path.split("/")
        ):
            raise ValueError("Gateway upstream contains an unsafe encoded or relative path.")
        if self.max_request_bytes < 1:
            raise ValueError("Gateway request limit must be positive.")
        if self.connect_timeout_seconds < 1 or self.response_timeout_seconds < 1:
            raise ValueError("Gateway timeouts must be positive.")

    @property
    def allowed_routes(self) -> frozenset[tuple[str, str]]:
        return _PROTOCOL_ROUTES[self.protocol]


@dataclass
class _ForwardState:
    """Mutable transfer state used to classify interrupted streams correctly."""

    status: int | None = None
    response_started: bool = False
    response_bytes: int = 0


class GatewayAuditLog:
    """Append metadata-only request records without provider bodies or headers."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._count = 0
        self._failure: str | None = None

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    def append(self, payload: dict[str, object]) -> None:
        with self._lock:
            if self._failure is not None:
                raise OSError(f"provider gateway audit is unavailable: {self._failure}")
            if self.path is None:
                self._count += 1
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.path.parent.chmod(0o700)
                except OSError:
                    pass
                encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    written = os.write(descriptor, encoded)
                    if written != len(encoded):
                        raise OSError(
                            "short gateway audit write: "
                            f"expected {len(encoded)}, wrote {written}"
                        )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self.path.chmod(0o600)
            except OSError as exc:
                self._failure = f"{type(exc).__name__}: {exc}"
                raise
            self._count += 1


class ProviderGatewayServer:
    """Threaded pinned-route proxy suitable for tests and a sidecar process."""

    def __init__(
        self,
        route: GatewayRoute,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        audit_path: Path | None = None,
    ) -> None:
        self.route = route
        self.audit = GatewayAuditLog(audit_path)
        self._server = _GatewayHTTPServer((host, port), _GatewayHandler, route, self.audit)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def base_url(self) -> str:
        host, port = self.address
        rendered = f"[{host}]" if ":" in host else host
        return f"http://{rendered}:{port}"

    @property
    def failure(self) -> str | None:
        return self.audit.failure

    def start(self) -> ProviderGatewayServer:
        if self._thread is not None:
            raise RuntimeError("Provider gateway is already running.")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ope-provider-gateway",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> ProviderGatewayServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        route: GatewayRoute,
        audit: GatewayAuditLog,
    ) -> None:
        self.route = route
        self.audit = audit
        self._audit_shutdown_started = threading.Event()
        super().__init__(address, handler)

    def fail_after_audit_error(self) -> None:
        """Stop accepting provider traffic after durable audit capture fails."""

        if self._audit_shutdown_started.is_set():
            return
        self._audit_shutdown_started.set()
        threading.Thread(
            target=self.shutdown,
            name="ope-provider-gateway-audit-failure",
            daemon=True,
        ).start()


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _GatewayHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/_optiprofiler/health":
            if self.server.audit.failure is None:
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(503, {"status": "audit_unavailable"})
            return
        self._proxy_request("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._proxy_request("POST")

    def do_CONNECT(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed"})

    def do_HEAD(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed"})

    def do_PATCH(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed"})

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed"})

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _proxy_request(self, method: str) -> None:
        request_id = secrets.token_hex(12)
        started_at = _utc_now()
        started = time.monotonic()
        path = "<rejected>"
        request_bytes = 0
        response_bytes = 0
        status = 502
        outcome = "gateway_error"
        error_type: str | None = None
        forward = _ForwardState()
        try:
            path = self._validated_path(method)
            body = self._read_request_body(method)
            request_bytes = len(body)
            status, response_bytes = self._forward(method, path, body, forward)
            outcome = "completed"
        except _GatewayRejection as exc:
            status = exc.status
            outcome = "rejected"
            error_type = exc.code
            self._send_json(exc.status, {"error": exc.code})
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            error_type = type(exc).__name__
            status = forward.status if forward.status is not None else 502
            response_bytes = forward.response_bytes
            if forward.response_started:
                outcome = "stream_interrupted"
                self.close_connection = True
            elif not self.wfile.closed:
                try:
                    self._send_json(502, {"error": "upstream_unavailable"})
                except OSError:
                    pass
        finally:
            try:
                self.server.audit.append(
                    {
                        "schema": "provider_gateway_request/1",
                        "request_id": request_id,
                        "protocol": self.server.route.protocol,
                        "method": method,
                        "path": path,
                        "started_at": started_at,
                        "finished_at": _utc_now(),
                        "duration_seconds": max(0.0, time.monotonic() - started),
                        "outcome": outcome,
                        "status": status,
                        "request_bytes": request_bytes,
                        "response_bytes": response_bytes,
                        "error_type": error_type,
                    }
                )
            except OSError:
                self.server.fail_after_audit_error()

    def _validated_path(self, method: str) -> str:
        if any(value is not None for value in (self.headers.get("Transfer-Encoding"),)):
            raise _GatewayRejection(400, "transfer_encoding_not_allowed")
        if len(self.headers.get_all("Host", [])) > 1:
            raise _GatewayRejection(400, "duplicate_host")
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment or target.query:
            raise _GatewayRejection(400, "absolute_or_qualified_target_not_allowed")
        if "%" in target.path or unquote(target.path) != target.path:
            raise _GatewayRejection(400, "encoded_path_not_allowed")
        if (method, target.path) not in self.server.route.allowed_routes:
            raise _GatewayRejection(404, "route_not_allowed")
        return target.path

    def _read_request_body(self, method: str) -> bytes:
        values = self.headers.get_all("Content-Length", [])
        if len(values) > 1:
            raise _GatewayRejection(400, "duplicate_content_length")
        if not values:
            if method == "POST":
                raise _GatewayRejection(411, "content_length_required")
            return b""
        try:
            size = int(values[0])
        except ValueError as exc:
            raise _GatewayRejection(400, "invalid_content_length") from exc
        if size < 0:
            raise _GatewayRejection(400, "invalid_content_length")
        if size > self.server.route.max_request_bytes:
            raise _GatewayRejection(413, "request_too_large")
        body = self.rfile.read(size)
        if len(body) != size:
            raise _GatewayRejection(400, "incomplete_request_body")
        return body

    def _forward(
        self,
        method: str,
        path: str,
        body: bytes,
        state: _ForwardState,
    ) -> tuple[int, int]:
        route = self.server.route
        parsed = urlsplit(route.upstream_base_url)
        _assert_allowed_upstream(parsed.hostname or "", parsed.port, parsed.scheme, route)
        headers = _forward_headers(self.headers.items())
        if route.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {route.credential}"
        else:
            headers["X-Api-Key"] = route.credential
        headers["Content-Length"] = str(len(body))
        target = _upstream_target(parsed.path, path)
        connection_class = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            parsed.hostname,
            parsed.port,
            timeout=route.connect_timeout_seconds,
        )
        try:
            connection.request(method, target, body=body if body else None, headers=headers)
            if connection.sock is not None:
                connection.sock.settimeout(route.response_timeout_seconds)
            response = connection.getresponse()
            state.status = response.status
            state.response_started = True
            self.send_response(response.status)
            for name, value in _filtered_response_headers(response.getheaders()):
                self.send_header(name, value)
            self.send_header("X-OptiProfiler-Gateway", "1")
            self.send_header("Connection", "close")
            self.end_headers()
            count = 0
            while chunk := response.read1(_CHUNK_BYTES):
                self.wfile.write(chunk)
                self.wfile.flush()
                count += len(chunk)
                state.response_bytes = count
            if response.length not in {None, 0}:
                raise http.client.IncompleteRead(b"", response.length)
            self.close_connection = True
            return response.status, count
        finally:
            connection.close()

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True


class _GatewayRejection(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _forward_headers(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    entries = [(str(name), str(value)) for name, value in items]
    connection_tokens = {
        token.strip().lower()
        for name, value in entries
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    }
    stripped = _WORKER_STRIPPED_HEADERS | connection_tokens
    forwarded: dict[str, str] = {}
    for name, value in entries:
        lowered = name.lower()
        if lowered in stripped or lowered.startswith("x-forwarded-"):
            continue
        if lowered == "content-length":
            continue
        if "\r" in value or "\n" in value:
            continue
        forwarded[name] = value
    return forwarded


def _filtered_response_headers(
    items: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    entries = tuple((str(name), str(value)) for name, value in items)
    connection_tokens = {
        token.strip().lower()
        for name, value in entries
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    }
    stripped = _RESPONSE_STRIPPED_HEADERS | connection_tokens
    return tuple((name, value) for name, value in entries if name.lower() not in stripped)


def _upstream_target(base_path: str, request_path: str) -> str:
    base = base_path.rstrip("/")
    if not base or request_path == base or request_path.startswith(base + "/"):
        return request_path
    return base + request_path


def _assert_allowed_upstream(
    hostname: str,
    port: int | None,
    scheme: str,
    route: GatewayRoute,
) -> None:
    if route.allow_private_upstream:
        return
    resolved = socket.getaddrinfo(
        hostname,
        port or (443 if scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    addresses = {item[4][0].split("%", 1)[0] for item in resolved}
    if not addresses:
        raise ValueError("Provider gateway could not resolve its pinned upstream.")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("Provider gateway upstream resolved to a non-public address.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_ready_file(path: Path, server: ProviderGatewayServer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "provider_gateway_ready/1",
        "base_url": server.base_url,
        "pid": os.getpid(),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")


def _parse_listen(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("listen address must be HOST:PORT")
    try:
        return host, int(port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("listen port must be an integer") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the internal provider gateway sidecar.")
    parser.add_argument("--listen", type=_parse_listen, default=("0.0.0.0", 8080))
    parser.add_argument("--protocol", choices=sorted(_PROTOCOL_ROUTES), required=True)
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--credential-env", required=True)
    parser.add_argument("--auth-mode", choices=("bearer", "x-api-key"), required=True)
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--max-request-bytes", type=int, default=16_000_000)
    parser.add_argument("--connect-timeout-seconds", type=int, default=15)
    parser.add_argument("--response-timeout-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    credential = os.environ.get(args.credential_env)
    if not credential:
        parser.error(f"credential environment variable {args.credential_env!r} is missing")
    route = GatewayRoute(
        protocol=args.protocol,
        upstream_base_url=args.upstream_base_url,
        credential=credential,
        auth_mode=args.auth_mode,
        max_request_bytes=args.max_request_bytes,
        connect_timeout_seconds=args.connect_timeout_seconds,
        response_timeout_seconds=args.response_timeout_seconds,
    )
    host, port = args.listen
    server = ProviderGatewayServer(route, host=host, port=port, audit_path=args.audit_log)

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.close, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.start()
    _write_ready_file(args.ready_file, server)
    try:
        while server._thread is not None and server._thread.is_alive():
            server._thread.join(timeout=1)
    finally:
        server.close()
    return 1 if server.failure is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__: list[str] = []
