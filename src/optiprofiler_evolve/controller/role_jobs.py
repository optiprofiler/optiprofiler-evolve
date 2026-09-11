"""Controller preparation of isolated research and integrity-review jobs."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import MappingProxyType

from optiprofiler_evolve.agents.broker import BrokerConnection
from optiprofiler_evolve.agents.contracts import (
    AgentJob,
    AgentJobResult,
    WorkerOutcome,
    WorkerRequest,
)
from optiprofiler_evolve.agents.jobs import copy_role_input as _copy_role_input
from optiprofiler_evolve.agents.jobs import redact_role_input as _redact_role_input
from optiprofiler_evolve.agents.jobs import safe_identifier as _safe_identifier
from optiprofiler_evolve.agents.jobs import worker_exit_warning as _worker_exit_warning
from optiprofiler_evolve.agents.llm import LLMClient
from optiprofiler_evolve.config import SandboxConfig, ToolConfig, WorkersConfig
from optiprofiler_evolve.errors import RunCancelled as _RunCancelled
from optiprofiler_evolve.files import safe_relative_path
from optiprofiler_evolve.solver import validate_tree_safety


class RoleJobs:
    """Prepare sanitized research and review jobs without granting evaluation access."""

    def __init__(
        self, *, initial: Path, run_dir: Path, workers: WorkersConfig,
        sandbox: SandboxConfig, llm_client: Callable[[WorkerRequest], LLMClient],
        cancellation_event: Event, emit: Callable,
    ) -> None:
        self.initial = initial
        self.run_dir = run_dir
        self.workers = workers
        self.sandbox = sandbox
        self.llm_client = llm_client
        self.cancellation_event = cancellation_event
        self.emit = emit

    def run(self, job: AgentJob) -> AgentJobResult:
        role = _safe_identifier(job.role, "role")
        job_id = _safe_identifier(job.job_id, "job_id")
        if job.worker is None and not 0 <= job.worker_index < len(self.workers.pool):
            raise ValueError(f"Agent job worker_index is out of range: {job.worker_index}")
        workspace = self.run_dir / "research" / "roles" / role / job_id
        if workspace.exists():
            raise FileExistsError(f"Role workspace already exists: {workspace}")
        workspace.mkdir(parents=True)
        replacements = {
            str(self.run_dir): "<run_dir>",
            str(self.initial): "<initial_solver>",
        }
        for relative, source in job.inputs.items():
            destination = workspace / safe_relative_path(relative)
            _copy_role_input(Path(source), destination)
            if relative not in {"seed", "finalist", "parent", "candidate"}:
                _redact_role_input(destination, replacements)
        validate_tree_safety(
            workspace,
            max_files=self.sandbox.max_candidate_files * 8,
            max_bytes=self.sandbox.max_candidate_bytes * 8,
        )

        tools = job.tools or ToolConfig(
            preset="minimal",
            web_search=False,
            network=False,
            compilers=False,
        )
        tools.validate()
        worker = job.worker or self.workers.pool[job.worker_index]
        worker.validate()
        workers = replace(
            self.workers,
            pool=(worker,),
            tools=tools,
            timeout_seconds=job.timeout_seconds or self.workers.timeout_seconds,
            token_budget=(
                job.token_budget
                if job.token_budget is not None
                else self.workers.token_budget
            ),
            max_budget_usd=(
                job.max_budget_usd
                if job.max_budget_usd is not None
                else self.workers.max_budget_usd
            ),
        )
        tools_dir = self.run_dir / "controller" / "role_tools" / role / job_id
        tools_dir.mkdir(parents=True, exist_ok=True)
        broker_root = self.run_dir / "controller" / "role_brokers" / role / job_id
        exchange = broker_root / "exchange"
        artifacts = broker_root / "artifacts"
        (exchange / "requests").mkdir(parents=True, exist_ok=True)
        (exchange / "responses").mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        if self.sandbox.backend == "docker":
            broker_directory = "/opt/optiprofiler-evolve/broker"
            artifacts_directory = "/opt/optiprofiler-evolve/artifacts"
        else:
            broker_directory = str(exchange)
            artifacts_directory = str(artifacts)
        connection = BrokerConnection(
            broker_directory,
            artifacts_directory,
            secrets.token_urlsafe(32),
            exchange,
            artifacts,
        )
        transcript = self.run_dir / "research" / "transcripts" / role / f"{job_id}.jsonl"
        role_phase = {
            "direction-scout": "direction_scout",
            "strategy-analyst": "strategy_analysis",
            "strategy-integrator": "strategy_transfer",
        }.get(role, role)
        scope: dict[str, object] = {"phase": role_phase, "role": role, "job_id": job_id}
        trace_links = dict(job.trace_links)
        reserved_links = {"phase", "module", "role", "job_id"}
        for key, value in trace_links.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in reserved_links:
                raise ValueError(f"Invalid or reserved trace link key: {key!r}")
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise TypeError(f"Trace link {key!r} must contain one scalar value.")
        island_match = re.search(r"island-(\d+)", job_id)
        if island_match:
            scope["island"] = int(island_match.group(1))
        if "island" in trace_links:
            linked_island = trace_links["island"]
            if not isinstance(linked_island, int) or isinstance(linked_island, bool):
                raise TypeError("Trace link 'island' must be an integer.")
            if "island" in scope and scope["island"] != linked_island:
                raise ValueError("Agent job island conflicts with its trace link.")
            scope["island"] = linked_island
        self.emit(
            "role_agent_started",
            "running",
            scope=scope,
            data={
                "worker": f"{worker.harness}:{worker.model}",
                "tools": {
                    "network": tools.network,
                    "web_search": tools.web_search,
                    "shell": tools.shell,
                },
                "prompt_hash": hashlib.sha256(job.prompt.encode()).hexdigest(),
            },
        )
        try:
            client = self.llm_client(
                WorkerRequest(
                    worker=worker,
                    workers=workers,
                    sandbox=self.sandbox,
                    workspace=workspace,
                    tools_dir=tools_dir,
                    broker=connection,
                    prompt=job.prompt,
                    transcript=transcript,
                    trace_dir=self.run_dir / "research" / "traces" / role / job_id,
                    trace_context={
                        "schema": "trace_input/1",
                        "join": {
                            "job_id": job_id,
                            "role": role,
                            "phase": role_phase,
                            "module": role_phase,
                            "island": scope.get("island"),
                            **trace_links,
                        },
                        "inputs": sorted(job.inputs),
                    },
                    cancellation_event=self.cancellation_event,
                )
            )
            result = client.run(job)
            outcome = result.outcome
        except Exception as exc:
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                f"[controller] role launch failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            outcome = WorkerOutcome(1, transcript)
            result = AgentJobResult(workspace, outcome, {}, job.expected_outputs, False)
        validate_tree_safety(
            workspace,
            max_files=self.sandbox.max_candidate_files * 8,
            max_bytes=self.sandbox.max_candidate_bytes * 8,
        )
        outputs = dict(result.outputs)
        missing_outputs = list(result.missing_outputs)
        succeeded = result.success
        self.emit(
            "role_agent_finished",
            "cancelled" if outcome.cancelled else "succeeded" if succeeded else "failed",
            scope=scope,
            data={
                "returncode": outcome.returncode,
                "worker_exit_warning": _worker_exit_warning(outcome),
                "timed_out": outcome.timed_out,
                "cancelled": outcome.cancelled,
                "termination_reason": outcome.termination_reason,
                "transcript": str(outcome.transcript),
                "native_trace": str(outcome.native_trace) if outcome.native_trace else None,
                "stderr_trace": str(outcome.stderr_trace) if outcome.stderr_trace else None,
                "trace_chunks": str(outcome.trace_chunks) if outcome.trace_chunks else None,
                "trace_outcome": str(outcome.trace_outcome) if outcome.trace_outcome else None,
                "trace_capture_error": outcome.capture_error,
                "provider_gateway_outcome": outcome.provider_gateway_outcome,
                "provider_gateway_request_count": outcome.provider_gateway_request_count,
                "provider_gateway_manifest": (
                    str(outcome.provider_gateway_manifest)
                    if outcome.provider_gateway_manifest
                    else None
                ),
                "usage": outcome.usage.as_dict(),
                "outputs": sorted(outputs),
                "missing_outputs": missing_outputs,
                "recovered_nonzero_exit": bool(
                    outcome.returncode != 0 and job.recover_outputs_on_nonzero_exit and succeeded
                ),
            },
        )
        if outcome.cancelled:
            raise _RunCancelled("Trusted role agent cancelled by controller signal.")
        if not succeeded:
            details = []
            if outcome.timed_out:
                details.append("timed_out=true")
            if outcome.cancelled:
                details.append("cancelled=true")
            if missing_outputs:
                details.append(f"missing_outputs={missing_outputs!r}")
            if outcome.returncode != 0:
                details.append(f"returncode={outcome.returncode}")
            raise RuntimeError(
                f"Trusted role {role!r} did not complete successfully ({', '.join(details)})."
            )
        return AgentJobResult(workspace, outcome, MappingProxyType(outputs))
