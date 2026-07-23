from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import optiprofiler_evolve.provider_gateway as provider_gateway_module
from optiprofiler_evolve.config import ProviderGatewayConfig, WorkerConfig
from optiprofiler_evolve.provider_gateway import GatewayRoute, ProviderGatewayServer
from optiprofiler_evolve.provider_transport import prepare_provider_transport


class _FakeUpstream:
    def __init__(
        self,
        responder: Callable[[BaseHTTPRequestHandler, bytes], None] | None = None,
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._responder = responder or self._default_response
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                owner._record_and_respond(self, b"")

            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers.get("Content-Length", "0"))
                owner._record_and_respond(self, self.rfile.read(size))

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> _FakeUpstream:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _record_and_respond(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        with self._lock:
            self.requests.append(
                {
                    "method": handler.command,
                    "path": handler.path,
                    "headers": {key.lower(): value for key, value in handler.headers.items()},
                    "body": body,
                }
            )
        self._responder(handler, body)

    @staticmethod
    def _default_response(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
        payload = b'{"ok":true}\n'
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


def _route(
    upstream: str,
    *,
    protocol: str = "anthropic",
    auth_mode: str = "x-api-key",
    max_request_bytes: int = 1024,
) -> GatewayRoute:
    return GatewayRoute(
        protocol=protocol,
        upstream_base_url=upstream,
        credential="real-provider-secret",
        auth_mode=auth_mode,
        max_request_bytes=max_request_bytes,
        connect_timeout_seconds=2,
        response_timeout_seconds=2,
        allow_private_upstream=True,
    )


def _request(
    gateway: ProviderGatewayServer,
    method: str,
    path: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    host, port = gateway.address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    request_headers = dict(headers or {})
    if method == "POST":
        request_headers.setdefault("Content-Length", str(len(body)))
    connection.request(method, path, body=body if body else None, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    result_headers = {key.lower(): value for key, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, payload, result_headers


class ProviderTransportTests(unittest.TestCase):
    def test_claude_plan_keeps_real_credential_out_of_worker(self) -> None:
        worker = WorkerConfig(
            harness="claude",
            model="test",
            env={
                "ANTHROPIC_AUTH_TOKEN": "real-provider-secret",
                "VISIBLE_SETTING": "visible",
            },
            provider_gateway=ProviderGatewayConfig(
                upstream_base_url="https://provider.example/anthropic",
                credential_env="ANTHROPIC_AUTH_TOKEN",
                auth_mode="bearer",
            ),
        )
        plan = prepare_provider_transport(
            worker,
            worker.env,
            gateway_origin="http://provider-gateway:8080",
        )

        self.assertEqual(plan.route.credential, "real-provider-secret")
        self.assertEqual(plan.route.auth_mode, "bearer")
        self.assertEqual(
            plan.worker.env["ANTHROPIC_BASE_URL"],
            "http://provider-gateway:8080",
        )
        self.assertEqual(plan.worker.env["VISIBLE_SETTING"], "visible")
        self.assertNotEqual(plan.worker.env["ANTHROPIC_API_KEY"], "real-provider-secret")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", plan.worker.env)
        self.assertNotIn("real-provider-secret", repr(plan))
        self.assertEqual(plan.worker.pass_env, ())
        self.assertIsNone(plan.worker.provider_gateway)

    def test_codex_plan_uses_responses_provider_at_gateway(self) -> None:
        worker = WorkerConfig(
            harness="codex",
            model="test",
            pass_env=("OPENAI_API_KEY",),
            provider_gateway=ProviderGatewayConfig(
                upstream_base_url="https://api.openai.com/v1",
                credential_env="OPENAI_API_KEY",
            ),
        )
        plan = prepare_provider_transport(
            worker,
            {"OPENAI_API_KEY": "real-provider-secret"},
            gateway_origin="http://provider-gateway:8080",
        )

        joined = " ".join(plan.worker.args)
        self.assertEqual(plan.route.protocol, "openai_responses")
        self.assertIn("--ignore-user-config", plan.worker.args)
        self.assertIn('model_provider="optiprofiler_gateway"', plan.worker.args)
        self.assertIn(
            'model_providers.optiprofiler_gateway.base_url="http://provider-gateway:8080/v1"',
            plan.worker.args,
        )
        self.assertIn('wire_api="responses"', joined)
        self.assertNotIn("real-provider-secret", joined)
        self.assertNotIn("real-provider-secret", plan.worker.env.values())

    def test_unrelated_secret_is_rejected_instead_of_entering_worker(self) -> None:
        worker = WorkerConfig(
            harness="claude",
            model="test",
            pass_env=("ANTHROPIC_API_KEY", "EXTRA_SECRET"),
            provider_gateway=ProviderGatewayConfig(
                upstream_base_url="https://api.anthropic.com",
                credential_env="ANTHROPIC_API_KEY",
            ),
        )
        with self.assertRaisesRegex(ValueError, "unrelated secret"):
            prepare_provider_transport(
                worker,
                {
                    "ANTHROPIC_API_KEY": "real-provider-secret",
                    "EXTRA_SECRET": "must-not-enter-worker",
                },
                gateway_origin="http://provider-gateway:8080",
            )


class ProviderGatewayTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "SIGTERM lifecycle requires POSIX")
    def test_process_marks_an_inflight_request_interrupted_on_sigterm(self) -> None:
        request_started = threading.Event()
        release_upstream = threading.Event()

        def block(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
            request_started.set()
            release_upstream.wait(timeout=10)
            handler.close_connection = True
            try:
                _FakeUpstream._default_response(handler, b"")
            except OSError:
                pass

        with _FakeUpstream(block) as upstream, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            environment = dict(os.environ)
            environment["TEST_PROVIDER_KEY"] = "real-provider-secret"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(provider_gateway_module.__file__).resolve()),
                    "--listen",
                    f"127.0.0.1:{port}",
                    "--protocol",
                    "anthropic",
                    "--upstream-base-url",
                    upstream.base_url,
                    "--credential-env",
                    "TEST_PROVIDER_KEY",
                    "--auth-mode",
                    "x-api-key",
                    "--audit-log",
                    str(root / "requests.jsonl"),
                    "--ready-file",
                    str(root / "ready.json"),
                    "--outcome-file",
                    str(root / "outcome.json"),
                    "--allow-private-upstream-for-tests",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            requester: threading.Thread | None = None
            try:
                deadline = time.monotonic() + 5
                while not (root / "ready.json").is_file():
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(f"gateway exited before ready: {stdout}\n{stderr}")
                    if time.monotonic() >= deadline:
                        self.fail("gateway process did not become ready")
                    time.sleep(0.01)

                def request() -> None:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        connection.request(
                            "POST",
                            "/v1/messages",
                            body=b"{}",
                            headers={"Content-Length": "2"},
                        )
                        connection.getresponse().read()
                    except (OSError, http.client.HTTPException):
                        pass
                    finally:
                        connection.close()

                requester = threading.Thread(target=request, daemon=True)
                requester.start()
                self.assertTrue(request_started.wait(timeout=5))
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 1)
            finally:
                release_upstream.set()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.communicate(timeout=1)
                if requester is not None:
                    requester.join(timeout=5)

            outcome = json.loads((root / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "interrupted")
            self.assertEqual(outcome["exit_code"], 1)
            self.assertGreaterEqual(outcome["inflight_request_count"], 1)

    def test_pins_route_rewrites_auth_and_strips_forwarding_headers(self) -> None:
        with _FakeUpstream() as upstream, tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "gateway.jsonl"
            route = _route(upstream.base_url + "/anthropic")
            with ProviderGatewayServer(route, audit_path=audit) as gateway:
                status, payload, headers = _request(
                    gateway,
                    "POST",
                    "/v1/messages",
                    b'{"prompt":"private-body"}',
                    {
                        "Authorization": "Bearer worker-controlled",
                        "X-Api-Key": "worker-controlled",
                        "X-Forwarded-Host": "metadata.internal",
                        "Connection": "X-Leak",
                        "X-Leak": "must-be-stripped",
                        "Host": "worker-controlled.invalid",
                        "Content-Type": "application/json",
                    },
                )

            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload), {"ok": True})
            self.assertEqual(headers["x-optiprofiler-gateway"], "1")
            received = upstream.requests[0]
            self.assertEqual(received["path"], "/anthropic/v1/messages")
            upstream_headers = received["headers"]
            assert isinstance(upstream_headers, dict)
            self.assertEqual(upstream_headers["x-api-key"], "real-provider-secret")
            self.assertNotIn("authorization", upstream_headers)
            self.assertNotIn("x-forwarded-host", upstream_headers)
            self.assertNotIn("x-leak", upstream_headers)
            self.assertNotEqual(upstream_headers["host"], "worker-controlled.invalid")

            audit_text = audit.read_text(encoding="utf-8")
            self.assertNotIn("real-provider-secret", audit_text)
            self.assertNotIn("private-body", audit_text)
            record = json.loads(audit_text)
            self.assertEqual(record["schema"], "provider_gateway_request/1")
            self.assertEqual(record["path"], "/v1/messages")

    def test_openai_responses_uses_bearer_and_does_not_duplicate_v1(self) -> None:
        with _FakeUpstream() as upstream:
            route = _route(
                upstream.base_url + "/v1",
                protocol="openai_responses",
                auth_mode="bearer",
            )
            with ProviderGatewayServer(route) as gateway:
                status, _, _ = _request(
                    gateway,
                    "POST",
                    "/v1/responses",
                    b"{}",
                )

            self.assertEqual(status, 200)
            received = upstream.requests[0]
            self.assertEqual(received["path"], "/v1/responses")
            headers = received["headers"]
            assert isinstance(headers, dict)
            self.assertEqual(headers["authorization"], "Bearer real-provider-secret")
            self.assertNotIn("x-api-key", headers)

    def test_rejects_unknown_absolute_connect_and_oversized_requests(self) -> None:
        with _FakeUpstream() as upstream:
            with ProviderGatewayServer(
                _route(upstream.base_url, max_request_bytes=4)
            ) as gateway:
                self.assertEqual(_request(gateway, "GET", "/admin")[0], 404)
                self.assertEqual(
                    _request(gateway, "POST", "/v1/messages", b"12345")[0],
                    413,
                )
                host, port = gateway.address
                with socket.create_connection((host, port), timeout=5) as connection:
                    connection.sendall(
                        b"GET http://metadata.internal/v1/models HTTP/1.1\r\n"
                        b"Host: gateway\r\nConnection: close\r\n\r\n"
                    )
                    absolute_response = connection.recv(4096)
                self.assertIn(b" 400 ", absolute_response.split(b"\r\n", 1)[0])
                with socket.create_connection((host, port), timeout=5) as connection:
                    connection.sendall(
                        b"CONNECT metadata.internal:80 HTTP/1.1\r\n"
                        b"Host: gateway\r\nConnection: close\r\n\r\n"
                    )
                    connect_response = connection.recv(4096)
                self.assertIn(b" 405 ", connect_response.split(b"\r\n", 1)[0])

            self.assertEqual(upstream.requests, [])

    def test_streams_sse_without_buffering_the_complete_response(self) -> None:
        first = b"data: first\n\n"
        second = b"data: second\n\n"

        def stream(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(first)
            handler.wfile.flush()
            time.sleep(0.35)
            handler.wfile.write(second)
            handler.wfile.flush()
            handler.close_connection = True

        with _FakeUpstream(stream) as upstream:
            with ProviderGatewayServer(_route(upstream.base_url)) as gateway:
                host, port = gateway.address
                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.request(
                    "POST",
                    "/v1/messages",
                    body=b"{}",
                    headers={"Content-Length": "2"},
                )
                response = connection.getresponse()
                started = time.monotonic()
                first_line = response.readline()
                first_elapsed = time.monotonic() - started
                rest = response.read()
                connection.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(first_line, b"data: first\n")
            self.assertLess(first_elapsed, 0.25)
            self.assertIn(second, rest)

    def test_records_an_interrupted_stream_without_sending_a_second_response(self) -> None:
        def truncate(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", "128")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b"data: partial\n\n")
            handler.wfile.flush()
            handler.close_connection = True

        with _FakeUpstream(truncate) as upstream, tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "gateway.jsonl"
            with ProviderGatewayServer(_route(upstream.base_url), audit_path=audit) as gateway:
                host, port = gateway.address
                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.request(
                    "POST",
                    "/v1/messages",
                    body=b"{}",
                    headers={"Content-Length": "2"},
                )
                response = connection.getresponse()
                with self.assertRaises(http.client.IncompleteRead):
                    response.read()
                connection.close()

            record = json.loads(audit.read_text(encoding="utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(record["status"], 200)
            self.assertEqual(record["outcome"], "stream_interrupted")
            self.assertEqual(record["error_type"], "IncompleteRead")
            self.assertGreater(record["response_bytes"], 0)

    def test_does_not_follow_upstream_redirects(self) -> None:
        secret_hits: list[str] = []

        def secret(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
            secret_hits.append(handler.path)
            _FakeUpstream._default_response(handler, b"")

        with _FakeUpstream(secret) as secret_upstream:
            def redirect(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
                handler.send_response(302)
                handler.send_header("Location", secret_upstream.base_url + "/secret")
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            with _FakeUpstream(redirect) as upstream:
                with ProviderGatewayServer(_route(upstream.base_url)) as gateway:
                    status, _, _ = _request(
                        gateway,
                        "POST",
                        "/v1/messages",
                        b"{}",
                    )

        self.assertEqual(status, 302)
        self.assertEqual(secret_hits, [])

    def test_private_upstream_is_denied_without_test_override(self) -> None:
        route = GatewayRoute(
            protocol="anthropic",
            upstream_base_url="https://127.0.0.1:9",
            credential="real-provider-secret",
            auth_mode="x-api-key",
            connect_timeout_seconds=1,
            response_timeout_seconds=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            with ProviderGatewayServer(route, audit_path=audit) as gateway:
                status, _, _ = _request(gateway, "GET", "/v1/models")
            record = json.loads(audit.read_text(encoding="utf-8"))
        self.assertEqual(status, 502)
        self.assertEqual(record["error_type"], "ValueError")

    def test_concurrent_requests_have_distinct_metadata_records(self) -> None:
        with _FakeUpstream() as upstream, tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            with ProviderGatewayServer(_route(upstream.base_url), audit_path=audit) as gateway:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    statuses = list(
                        pool.map(
                            lambda _index: _request(
                                gateway,
                                "POST",
                                "/v1/messages",
                                b"{}",
                            )[0],
                            range(16),
                        )
                    )

            records = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual(statuses, [200] * 16)
            self.assertEqual(len(records), 16)
            self.assertEqual(len({record["request_id"] for record in records}), 16)

    def test_audit_write_failure_stops_the_gateway(self) -> None:
        with _FakeUpstream() as upstream, tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit-as-directory"
            audit.mkdir()
            gateway = ProviderGatewayServer(_route(upstream.base_url), audit_path=audit)
            gateway.start()
            try:
                self.assertEqual(
                    _request(gateway, "POST", "/v1/messages", b"{}")[0],
                    200,
                )
                deadline = time.monotonic() + 2
                while gateway._thread is not None and gateway._thread.is_alive():
                    if time.monotonic() >= deadline:
                        self.fail("gateway did not stop after its audit log failed")
                    time.sleep(0.01)
                self.assertIsNotNone(gateway.failure)
                self.assertEqual(gateway.audit.count, 0)
            finally:
                gateway.close()

    def test_audit_directory_failure_is_also_terminal(self) -> None:
        with _FakeUpstream() as upstream, tempfile.TemporaryDirectory() as directory:
            blocking_file = Path(directory) / "not-a-directory"
            blocking_file.write_text("block", encoding="utf-8")
            audit = blocking_file / "gateway.jsonl"
            gateway = ProviderGatewayServer(_route(upstream.base_url), audit_path=audit)
            gateway.start()
            try:
                self.assertEqual(
                    _request(gateway, "POST", "/v1/messages", b"{}")[0],
                    200,
                )
                deadline = time.monotonic() + 2
                while gateway._thread is not None and gateway._thread.is_alive():
                    if time.monotonic() >= deadline:
                        self.fail("gateway did not stop after audit directory creation failed")
                    time.sleep(0.01)
                self.assertIn("FileExistsError", gateway.failure or "")
                self.assertEqual(gateway.audit.count, 0)
            finally:
                gateway.close()

    def test_audit_log_is_owner_only(self) -> None:
        with _FakeUpstream() as upstream, tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "gateway.jsonl"
            with ProviderGatewayServer(_route(upstream.base_url), audit_path=audit) as gateway:
                self.assertEqual(
                    _request(gateway, "POST", "/v1/messages", b"{}")[0],
                    200,
                )

            self.assertEqual(stat.S_IMODE(audit.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
