"""Execute coding-agent harnesses in isolated worker environments."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .broker import BrokerConnection
from .config import SandboxConfig, WorkerConfig, WorkersConfig
from .docker_runtime import DockerInvocationRuntime, GatewayRuntimeOutcome
from .harness import build_harness_command
from .traces import (
    finalize_adapter_trace,
    prepare_trace,
    record_controller_failure,
    record_derived_trace_error,
    record_sanitized_command,
    record_transport_failure,
    render_transcript,
    run_captured_process,
    trace_paths,
)


@dataclass(frozen=True)
class AgentRunResult:
    returncode: int
    transcript: Path
    timed_out: bool = False
    native_trace: Path | None = None
    stderr_trace: Path | None = None
    trace_chunks: Path | None = None
    trace_outcome: Path | None = None
    capture_error: str | None = None
    cancelled: bool = False
    termination_reason: str | None = None
    provider_gateway_outcome: str | None = None
    provider_gateway_request_count: int | None = None
    provider_gateway_manifest: Path | None = None


def run_agent(
    *,
    worker: WorkerConfig,
    workers: WorkersConfig,
    sandbox: SandboxConfig,
    workspace: Path,
    tools_dir: Path,
    broker: BrokerConnection,
    prompt: str,
    transcript: Path,
    trace_dir: Path,
    trace_context: Mapping[str, object] | None = None,
    cancellation_event: threading.Event | None = None,
) -> AgentRunResult:
    """Run one coding worker and preserve its complete stdout/stderr transcript."""

    transcript.parent.mkdir(parents=True, exist_ok=True)
    selected_values = _selected_worker_values(worker)
    if trace_dir.exists():
        paths = trace_paths(trace_dir)
    else:
        paths = prepare_trace(
            root=trace_dir,
            prompt=prompt,
            command=["<worker-adapter>", "cli"],
            worker=worker,
            workers=workers,
            sandbox=sandbox,
            context=trace_context,
            secret_values=selected_values,
        )
    runtime: DockerInvocationRuntime | None = None
    gateway: GatewayRuntimeOutcome | None = None
    worker_started = False
    worker_finished = False
    try:
        if sandbox.backend == "docker":
            runtime = DockerInvocationRuntime(
                worker=worker,
                workers=workers,
                sandbox=sandbox,
                selected_values=selected_values,
                trace_dir=trace_dir,
            )
            effective_worker, effective_values = runtime.start()
            harness = build_harness_command(
                effective_worker,
                workers,
                workers.tools,
                Path("/workspace"),
            )
            command = runtime.worker_command(
                workspace=workspace,
                tools_dir=tools_dir,
                broker=broker,
                harness=harness,
                selected_values=effective_values,
            )
            environment = dict(os.environ)
            environment.update(effective_values)
            environment.update(
                {
                    "OPTIPROFILER_EVOLVE_BROKER_DIR": broker.directory,
                    "OPTIPROFILER_EVOLVE_BROKER_TOKEN": broker.token,
                    "OPTIPROFILER_EVOLVE_WORKSPACE": "/workspace",
                }
            )
            cwd = None
            command_secret_values = effective_values
        else:
            command = build_harness_command(worker, workers, workers.tools, workspace)
            environment = _worker_environment(selected_values, broker, tools_dir, workspace)
            cwd = workspace
            command_secret_values = selected_values
        record_sanitized_command(paths, command, command_secret_values)
        if runtime is not None:
            runtime.record_worker_started()
            worker_started = True
        completed = run_captured_process(
            command=command,
            prompt=prompt,
            paths=paths,
            timeout_seconds=workers.timeout_seconds,
            environment=environment,
            cwd=cwd,
            cancellation_event=cancellation_event,
        )
        if runtime is not None:
            runtime.record_worker_finished(
                returncode=completed.returncode,
                timed_out=completed.timed_out,
                cancelled=completed.cancelled,
                termination_reason=completed.termination_reason,
            )
            worker_finished = True
            runtime.ensure_worker_terminal()
            gateway = runtime.finish_gateway()
            if gateway is not None and not gateway.healthy:
                message = (
                    "provider gateway failed: "
                    f"outcome={gateway.outcome}, exit_code={gateway.exit_code}, "
                    f"audit_failure={gateway.audit_failure}"
                )
                record_transport_failure(paths, message)
                completed = replace(
                    completed,
                    returncode=completed.returncode or 1,
                    capture_error=_merge_errors(completed.capture_error, message),
                    termination_reason=(
                        completed.termination_reason
                        if completed.timed_out or completed.cancelled
                        else "provider_gateway_failure"
                    ),
                )
        try:
            render_transcript(paths, transcript)
        except (OSError, ValueError) as exc:
            message = f"transcript: {type(exc).__name__}: {exc}"
            record_derived_trace_error(paths, message)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                f"[controller] readable transcript unavailable: {message}\n",
                encoding="utf-8",
            )
        return AgentRunResult(
            completed.returncode,
            transcript,
            timed_out=completed.timed_out,
            native_trace=paths.stdout,
            stderr_trace=paths.stderr,
            trace_chunks=paths.chunks,
            trace_outcome=paths.outcome,
            capture_error=completed.capture_error,
            cancelled=completed.cancelled,
            termination_reason=completed.termination_reason,
            provider_gateway_outcome=gateway.outcome if gateway is not None else None,
            provider_gateway_request_count=(
                gateway.request_count if gateway is not None else None
            ),
            provider_gateway_manifest=(gateway.manifest if gateway is not None else None),
        )
    except Exception as exc:
        if runtime is not None and worker_started and not worker_finished:
            try:
                runtime.record_worker_finished(
                    returncode=1,
                    timed_out=False,
                    cancelled=cancellation_event.is_set() if cancellation_event else False,
                    termination_reason="worker_launch_or_capture_failure",
                )
            except OSError:
                pass
        if runtime is not None and gateway is None:
            try:
                gateway = runtime.finish_gateway()
            except Exception:
                pass
        transcript.parent.mkdir(parents=True, exist_ok=True)
        if not transcript.exists():
            transcript.write_text(
                f"[controller] agent execution failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
        finalize_adapter_trace(
            paths,
            transcript=transcript,
            native_trace=paths.stdout if paths.stdout.exists() else None,
            stderr_trace=paths.stderr if paths.stderr.exists() else None,
            returncode=1,
            timed_out=False,
            cancelled=cancellation_event.is_set() if cancellation_event else False,
            capture_error=f"execution: {type(exc).__name__}: {exc}",
        )
        failure = f"{type(exc).__name__}: {exc}"
        if runtime is not None and runtime.gateway_enabled:
            record_transport_failure(paths, f"provider gateway lifecycle failed: {failure}")
        else:
            record_controller_failure(paths, f"worker controller failed: {failure}")
        raise
    finally:
        if runtime is not None:
            try:
                cleanup_errors = runtime.cleanup()
                if cleanup_errors:
                    record_derived_trace_error(
                        paths,
                        "docker cleanup: " + "; ".join(cleanup_errors),
                    )
            except Exception as exc:
                try:
                    record_derived_trace_error(
                        paths,
                        f"docker cleanup: {type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass


def _worker_environment(
    selected_values: Mapping[str, str],
    broker: BrokerConnection,
    tools_dir: Path,
    workspace: Path,
) -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": f"{tools_dir}:{os.environ.get('PATH', '')}",
        "OPTIPROFILER_EVOLVE_BROKER_DIR": broker.directory,
        "OPTIPROFILER_EVOLVE_BROKER_TOKEN": broker.token,
        "OPTIPROFILER_EVOLVE_WORKSPACE": str(workspace.resolve()),
    }
    environment.update(selected_values)
    return environment


def _selected_worker_values(worker: WorkerConfig) -> Mapping[str, str]:
    values = dict(worker.env)
    for key in worker.pass_env:
        if key not in os.environ:
            raise ValueError(f"Required worker environment variable {key!r} is not set.")
        values[key] = os.environ[key]
    return values


def _merge_errors(left: str | None, right: str) -> str:
    return f"{left}; {right}" if left else right


__all__: list[str] = []
