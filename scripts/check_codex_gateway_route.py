#!/usr/bin/env python3
"""Prove that the installed Codex CLI reaches the pinned provider gateway."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from optiprofiler_evolve.config import ProviderGatewayConfig, ToolConfig, WorkerConfig, WorkersConfig
from optiprofiler_evolve.harness import build_harness_command
from optiprofiler_evolve.provider_gateway import GatewayRoute, ProviderGatewayServer
from optiprofiler_evolve.provider_transport import prepare_provider_transport


def main() -> int:
    executable = shutil.which("codex")
    if executable is None:
        raise SystemExit("codex is not available on PATH")
    received: list[dict[str, object]] = []
    lock = threading.Lock()

    class FakeResponses(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(size)
            with lock:
                received.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(body),
                    }
                )
            payload = _responses_stream()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponses)
    upstream.daemon_threads = True
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    upstream_host, upstream_port = upstream.server_address[:2]
    route = GatewayRoute(
        protocol="openai_responses",
        upstream_base_url=f"http://{upstream_host}:{upstream_port}/v1",
        credential="fake-upstream-key",
        auth_mode="bearer",
        allow_private_upstream=True,
    )
    try:
        with ProviderGatewayServer(route) as gateway, tempfile.TemporaryDirectory() as directory:
            worker = WorkerConfig(
                harness="codex",
                model="gateway-route-probe",
                env={"OPENAI_API_KEY": "fake-controller-key"},
                provider_gateway=ProviderGatewayConfig(
                    upstream_base_url="https://unused.invalid/v1",
                    credential_env="OPENAI_API_KEY",
                ),
            )
            plan = prepare_provider_transport(
                worker,
                worker.env,
                gateway_origin=gateway.base_url,
            )
            workers = WorkersConfig(
                pool=(plan.worker,),
                tools=ToolConfig(network=False, web_search=False),
            )
            workspace = Path(directory) / "workspace"
            home = Path(directory) / "home"
            workspace.mkdir()
            home.mkdir()
            command = build_harness_command(plan.worker, workers, workers.tools, workspace)
            command[0] = executable
            environment = {
                "HOME": str(home),
                "PATH": os.environ.get("PATH", ""),
                **dict(plan.worker.env),
            }
            completed = subprocess.run(
                command,
                input="Reply briefly that the gateway route probe passed. Do not call tools.",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=workspace,
                env=environment,
                timeout=30,
                check=False,
            )
    finally:
        upstream.shutdown()
        upstream_thread.join(timeout=5)
        upstream.server_close()

    if completed.returncode != 0:
        raise SystemExit(
            "Codex did not complete through the fake gateway:\n" + completed.stdout[-4000:]
        )
    if len(received) != 1 or received[0]["path"] != "/v1/responses":
        raise SystemExit(f"unexpected fake-upstream requests: {received!r}")
    if received[0]["authorization"] != "Bearer fake-upstream-key":
        raise SystemExit("gateway did not replace the worker credential")
    serialized = json.dumps(received, sort_keys=True)
    if "fake-controller-key" in serialized:
        raise SystemExit("worker-side credential reached the fake upstream")
    print("Codex gateway route preflight: ok")
    print("observed path: /v1/responses")
    return 0


def _responses_stream() -> bytes:
    now = int(time.time())
    message = {
        "id": "msg_gateway_probe",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": "Gateway route probe passed.",
                "annotations": [],
            }
        ],
    }
    response = {
        "id": "resp_gateway_probe",
        "object": "response",
        "created_at": now,
        "status": "completed",
        "completed_at": now,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": "gateway-route-probe",
        "output": [message],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": 1,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 6,
        },
        "metadata": {},
    }
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}, "sequence_number": 0},
        {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}, "sequence_number": 1},
        {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0, "content_index": 0, "delta": "Gateway route probe passed.", "sequence_number": 2},
        {"type": "response.output_text.done", "item_id": message["id"], "output_index": 0, "content_index": 0, "text": "Gateway route probe passed.", "sequence_number": 3},
        {"type": "response.output_item.done", "output_index": 0, "item": message, "sequence_number": 4},
        {"type": "response.completed", "response": response, "sequence_number": 5},
    ]
    return (
        "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        + "data: [DONE]\n\n"
    ).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
