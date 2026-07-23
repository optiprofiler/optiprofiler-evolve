from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path


RUN_DOCKER = os.environ.get("OPE_RUN_DOCKER_TESTS") == "1"
GATEWAY_IMAGE = os.environ.get(
    "OPE_GATEWAY_IMAGE",
    "optiprofiler-evolve-gateway:latest",
)


@unittest.skipUnless(RUN_DOCKER, "set OPE_RUN_DOCKER_TESTS=1 for Docker integration")
class DockerGatewayIntegrationTests(unittest.TestCase):
    def test_worker_has_gateway_transport_without_direct_egress_or_secret(self) -> None:
        suffix = uuid.uuid4().hex[:12]
        internal = f"ope-test-internal-{suffix}"
        egress = f"ope-test-egress-{suffix}"
        upstream = f"ope-test-upstream-{suffix}"
        gateway = f"ope-test-gateway-{suffix}"
        worker = f"ope-test-worker-{suffix}"
        secret = f"test-secret-{suffix}"
        objects = (worker, gateway, upstream)
        networks = (egress, internal)

        with tempfile.TemporaryDirectory(prefix=".docker-test-", dir=Path.cwd()) as directory:
            state = Path(directory) / "gateway"
            state.mkdir(mode=0o700)
            try:
                _run(["docker", "network", "create", "--internal", internal])
                _run(["docker", "network", "create", egress])
                _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        upstream,
                        "--network",
                        egress,
                        "--network-alias",
                        "fake-upstream",
                        "--entrypoint",
                        "python",
                        GATEWAY_IMAGE,
                        "-c",
                        _FAKE_UPSTREAM,
                    ]
                )
                _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        gateway,
                        "--network",
                        internal,
                        "--network-alias",
                        "provider-gateway",
                        "--user",
                        f"{os.getuid()}:{os.getgid()}",
                        "--mount",
                        f"type=bind,src={state},dst=/state",
                        "--env",
                        f"TEST_PROVIDER_KEY={secret}",
                        GATEWAY_IMAGE,
                        "--listen",
                        "0.0.0.0:8080",
                        "--protocol",
                        "openai_responses",
                        "--upstream-base-url",
                        "http://fake-upstream:8000",
                        "--credential-env",
                        "TEST_PROVIDER_KEY",
                        "--auth-mode",
                        "bearer",
                        "--audit-log",
                        "/state/requests.jsonl",
                        "--ready-file",
                        "/state/ready.json",
                        "--outcome-file",
                        "/state/outcome.json",
                        "--advertised-base-url",
                        "http://provider-gateway:8080",
                        "--allow-private-upstream-for-tests",
                    ]
                )
                _run(
                    [
                        "docker",
                        "network",
                        "connect",
                        "--gw-priority",
                        "1",
                        egress,
                        gateway,
                    ]
                )
                _wait_for_file(state / "ready.json")
                _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        worker,
                        "--network",
                        internal,
                        "--env",
                        "OPENAI_API_KEY=dummy",
                        "--entrypoint",
                        "python",
                        GATEWAY_IMAGE,
                        "-c",
                        _WORKER_PROBE,
                    ]
                )
                _wait_for_log(worker, "probe-ok")

                worker_inspect = _inspect(worker)
                worker_env = worker_inspect["Config"]["Env"]
                worker_networks = worker_inspect["NetworkSettings"]["Networks"]
                self.assertNotIn(secret, json.dumps(worker_env))
                self.assertEqual(set(worker_networks), {internal})
                audit = (state / "requests.jsonl").read_text(encoding="utf-8")
                self.assertIn('"path": "/v1/models"', audit)
                self.assertIn('"outcome": "completed"', audit)
                self.assertNotIn(secret, audit)

                _run(["docker", "stop", "--time", "5", gateway])
                outcome = json.loads((state / "outcome.json").read_text(encoding="utf-8"))
                self.assertEqual(outcome["outcome"], "completed")
                self.assertEqual(outcome["request_count"], 1)
                self.assertEqual(outcome["inflight_request_count"], 0)
            finally:
                for name in objects:
                    _run(["docker", "rm", "-f", name], check=False)
                for name in networks:
                    _run(["docker", "network", "rm", name], check=False)

            for name in objects:
                self.assertNotEqual(
                    _run(["docker", "inspect", name], check=False).returncode,
                    0,
                )
            for name in networks:
                self.assertNotEqual(
                    _run(["docker", "network", "inspect", name], check=False).returncode,
                    0,
                )


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"Docker command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr.strip()}"
        )
    return completed


def _inspect(name: str) -> dict[str, object]:
    payload = json.loads(_run(["docker", "inspect", name]).stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise AssertionError(f"unexpected Docker inspect payload for {name}")
    result = payload[0]
    if not isinstance(result, dict):
        raise AssertionError(f"unexpected Docker inspect object for {name}")
    return result


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_log(container: str, marker: str) -> None:
    deadline = time.monotonic() + 15
    last = ""
    while time.monotonic() < deadline:
        completed = _run(["docker", "logs", container], check=False)
        last = completed.stdout + completed.stderr
        if marker in last:
            return
        state = _inspect(container)["State"]
        if isinstance(state, dict) and not state.get("Running"):
            break
        time.sleep(0.1)
    raise AssertionError(f"worker probe failed before {marker!r}: {last}")


_FAKE_UPSTREAM = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"data": []}\n'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""


_WORKER_PROBE = r"""
import json
import os
import socket
import time
import urllib.request

with urllib.request.urlopen('http://provider-gateway:8080/_optiprofiler/health', timeout=5) as r:
    assert json.load(r)['status'] == 'ok'
with urllib.request.urlopen('http://provider-gateway:8080/v1/models', timeout=5) as r:
    assert json.load(r)['data'] == []
assert 'TEST_PROVIDER_KEY' not in os.environ
try:
    socket.getaddrinfo('fake-upstream', 8000)
except socket.gaierror:
    pass
else:
    raise AssertionError('worker resolved the egress-only upstream')
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('worker reached unrelated external egress')
print('probe-ok', flush=True)
time.sleep(30)
"""


if __name__ == "__main__":
    unittest.main()
