"""Capability-limited evaluation tools injected into worker sandboxes."""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .evaluation import Evaluator
from .models import EvaluationResult


_TOOL_CLIENT = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time
import uuid

mode = "smoke" if os.path.basename(sys.argv[0]) == "smoke_test" else "public"
root = pathlib.Path(os.environ["OPTIPROFILER_EVOLVE_BROKER_DIR"])
workspace = pathlib.Path(os.environ["OPTIPROFILER_EVOLVE_WORKSPACE"])
token = os.environ["OPTIPROFILER_EVOLVE_BROKER_TOKEN"]
request_id = uuid.uuid4().hex
request = root / "requests" / (request_id + ".json")
temporary = request.with_suffix(".tmp")
temporary.write_text(json.dumps({"id": request_id, "token": token, "mode": mode}))
temporary.replace(request)
response = root / "responses" / (request_id + ".json")
deadline = time.monotonic() + 86400
while not response.exists():
    if time.monotonic() >= deadline:
        print("evaluation broker timed out", file=sys.stderr)
        raise SystemExit(1)
    time.sleep(0.1)
payload = json.loads(response.read_text())
response.unlink(missing_ok=True)
result = payload["payload"]
if payload["status"] == 200:
    relative = pathlib.Path(result["output_dir"])
    output = workspace / relative
    output.mkdir(parents=True, exist_ok=True)
    feedback = result.pop("_feedback", "")
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\\n")
    (output / "feedback.md").write_text(feedback)
print(json.dumps(result, indent=2))
raise SystemExit(0 if payload["status"] == 200 and result.get("success") else 1)
"""


@dataclass(frozen=True)
class BrokerConnection:
    directory: str
    artifacts_directory: str
    token: str
    host_directory: Path
    host_artifacts_directory: Path


class EvaluationBroker:
    """Bind smoke/public capabilities to exactly one worker workspace."""

    def __init__(
        self,
        *,
        workspace: Path,
        control_dir: Path,
        evaluator: Evaluator,
        max_smoke_calls: int,
        max_public_calls: int,
        candidate_validator: Callable[[Path], None] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.evaluator = evaluator
        self.control_dir = control_dir.resolve()
        self.exchange = self.control_dir / "exchange"
        self.artifacts = self.control_dir / "artifacts"
        self.candidate_validator = candidate_validator
        self.quotas = {"smoke": max_smoke_calls, "public": max_public_calls}
        self.counts = {"smoke": 0, "public": 0}
        self.token = secrets.token_urlsafe(32)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker_artifacts = str(self.artifacts)

    def start(self, *, docker: bool) -> BrokerConnection:
        for name in ("requests", "responses"):
            (self.exchange / name).mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        if docker:
            directory = "/opt/optiprofiler-evolve/broker"
            artifacts = "/opt/optiprofiler-evolve/artifacts"
        else:
            directory = str(self.exchange)
            artifacts = str(self.artifacts)
        self._worker_artifacts = artifacts
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return BrokerConnection(directory, artifacts, self.token, self.exchange, self.artifacts)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._thread = None

    def __enter__(self) -> "EvaluationBroker":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.stop()

    def install_tools(self, tools_dir: Path) -> None:
        tools_dir.mkdir(parents=True, exist_ok=True)
        for name in ("smoke_test", "evaluate"):
            path = tools_dir / name
            path.write_text(_TOOL_CLIENT, encoding="utf-8")
            path.chmod(0o755)

    def _serve(self) -> None:
        requests = self.exchange / "requests"
        while not self._stop.is_set():
            if requests.is_symlink() or not requests.is_dir():
                return
            processed = False
            for request in sorted(requests.glob("*.json")):
                processed = True
                self._process(request)
            if not processed:
                self._stop.wait(0.05)

    def _process(self, request_path: Path) -> None:
        try:
            metadata = request_path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("request must be a regular file")
            if metadata.st_size > 16_384:
                raise ValueError("request is too large")
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request_id = request_path.stem
            if request.get("id") != request_id or len(request_id) != 32:
                self._respond(request_id, 400, {"error": "invalid request id"})
                return
            int(request_id, 16)
            if request.get("token") != self.token:
                self._respond(request_id, 401, {"error": "unauthorized"})
                return
            mode = request.get("mode")
            if mode in {"hidden", "final"}:
                self._respond(request_id, 403, {"error": "controller-only"})
                return
            if mode not in self.quotas:
                self._respond(request_id, 404, {"error": "unknown capability"})
                return
            if self.counts[mode] >= self.quotas[mode]:
                self._respond(request_id, 429, {"error": "quota exceeded"})
                return
            self.counts[mode] += 1
            call = self.counts[mode]
            if self.candidate_validator is not None:
                self.candidate_validator(self.workspace)
            output = self.artifacts / "evaluations" / mode / f"{call:03d}"
            result = self.evaluator.evaluate(self.workspace, mode, output)
            payload = result.as_dict()
            relative = Path(".optiprofiler_evolve") / "evaluations" / mode / f"{call:03d}"
            feedback = output / "feedback.md"
            payload.update(
                {
                    "output_dir": str(relative),
                    "result_file": str(relative / "result.json"),
                    "feedback_file": str(relative / "feedback.md"),
                    "benchmark_artifacts": str(
                        Path(self._artifacts_prefix()) / "evaluations" / mode / f"{call:03d}"
                    ),
                    "_feedback": feedback.read_text(encoding="utf-8") if feedback.exists() else "",
                }
            )
            self._respond(request_id, 200, payload)
        except Exception as exc:
            request_id = request_path.stem
            self._respond(request_id, 500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            request_path.unlink(missing_ok=True)

    def _respond(self, request_id: str, status: int, payload: dict[str, Any]) -> None:
        responses = self.exchange / "responses"
        if responses.is_symlink() or not responses.is_dir():
            raise RuntimeError("broker response directory was tampered with")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(responses, directory_flags)
        temporary = f".{request_id}.{secrets.token_hex(8)}.tmp"
        final = f"{request_id}.json"
        try:
            file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            file_flags |= getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(temporary, file_flags, 0o600, dir_fd=directory_fd)
            with os.fdopen(file_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"status": status, "payload": payload}) + "\n")
            os.replace(
                temporary,
                final,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)

    def _artifacts_prefix(self) -> str:
        return self._worker_artifacts


def latest_workspace_result(workspace: Path, mode: str) -> EvaluationResult | None:
    """Read the last worker-triggered result for prompt memory and diagnostics."""

    root = workspace / ".optiprofiler_evolve" / "evaluations" / mode
    paths = sorted(root.glob("*/result.json")) if root.is_dir() else []
    if not paths:
        return None
    raw = json.loads(paths[-1].read_text(encoding="utf-8"))
    raw = {
        key: value
        for key, value in raw.items()
        if key
        in {
            "mode",
            "score",
            "candidate_score",
            "reference_score",
            "problems",
            "output_dir",
            "success",
            "error",
            "profile_scores",
        }
    }
    raw["problems"] = tuple(raw["problems"])
    raw["output_dir"] = Path(raw["output_dir"])
    return EvaluationResult(**raw)


__all__: list[str] = []
