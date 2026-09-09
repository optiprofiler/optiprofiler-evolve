"""Deterministic controller for benchmark-driven solver evolution."""

from __future__ import annotations

import json
import random
import secrets
import signal
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from optiprofiler_evolve.agents.contracts import WorkerAdapter, WorkerOutcome, WorkerRequest
from optiprofiler_evolve.agents.docker_runtime import cleanup_managed_resources
from optiprofiler_evolve.agents.explanations import explanation_memory, explanation_status
from optiprofiler_evolve.agents.jobs import worker_exit_warning as _worker_exit_warning
from optiprofiler_evolve.agents.llm import LLMClient
from optiprofiler_evolve.config import EvolveConfig, WorkerConfig
from optiprofiler_evolve.controller.builtins import register_builtin_components
from optiprofiler_evolve.controller.contracts import ControllerServices, VariantHandle
from optiprofiler_evolve.controller.phase_contexts import bind_context, commit_result
from optiprofiler_evolve.controller.registry import build, resolve
from optiprofiler_evolve.controller.role_jobs import RoleJobs
from optiprofiler_evolve.controller.selection import (
    MetricSelectionError,
    ParentSampler,
    RetentionPolicy,
)
from optiprofiler_evolve.controller.services import EngineControllerServices
from optiprofiler_evolve.criteria.guidance import build_generation_guidance
from optiprofiler_evolve.criteria.integrity.execution import IntegrityReviewExecutor
from optiprofiler_evolve.criteria.performance.contracts import Evaluator
from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.errors import RunCancelled as _RunCancelled
from optiprofiler_evolve.files import append_json as _append_json
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import (
    Attempt,
    CandidateRecord,
    EvaluationResult,
    EvolveResult,
    FinalistResult,
)
from optiprofiler_evolve.observability.events import EventWriter
from optiprofiler_evolve.observability.projections import project_public_events
from optiprofiler_evolve.observability.provenance import build_run_provenance
from optiprofiler_evolve.observability.reports import append_trace_coverage
from optiprofiler_evolve.observability.trace_ledger import TraceLedger
from optiprofiler_evolve.observability.usage import UsageRecord, aggregate_usage
from optiprofiler_evolve.observability.views.owner import render_owner_views
from optiprofiler_evolve.observability.views.public import (
    materialize_public_bundle,
    render_final_report,
    render_public_report,
    render_status,
)
from optiprofiler_evolve.phases.attempts.contracts import (
    AfterIterationPolicy,
    AttemptStep,
    PopulationEdit,
)
from optiprofiler_evolve.phases.attempts.runner import AttemptRunner
from optiprofiler_evolve.phases.contracts import Phase, PhaseContext
from optiprofiler_evolve.solver import (
    InterfaceSpec,
    validate_candidate_imports,
    validate_interface,
    validate_tree_safety,
)


class EvolutionEngine:
    """Internal implementation behind the single public :func:`evolve` API."""

    def __init__(
        self,
        *,
        initial: str | Path,
        interface: InterfaceSpec,
        runtime: str,
        editable: Sequence[str],
        config: EvolveConfig,
        run_dir: Path,
        worker_adapter: WorkerAdapter | None = None,
        evaluator_factory: Callable[..., Evaluator] | None = None,
    ) -> None:
        register_builtin_components()
        self.initial = Path(initial).expanduser().resolve()
        self.interface = interface
        self.runtime = runtime
        self.editable = tuple(editable)
        self.config = config
        self.run_dir = run_dir.expanduser().resolve()
        self.worker_adapter = (
            worker_adapter
            if worker_adapter is not None
            else resolve("worker", config.workers.adapter)()
        )
        self.evaluator_factory = evaluator_factory
        if self.evaluator_factory is None and config.assessment.enabled("performance"):
            self.evaluator_factory = resolve("evaluator", config.execution.adapter)
        self.phases: tuple[Phase, ...] = tuple(
            build("phase", spec) for spec in config.workflow.phases
        )
        self.attempt_steps: tuple[AttemptStep, ...] = tuple(
            build("step", spec) for spec in config.workflow.attempt_steps
        )
        self.after_iteration: tuple[AfterIterationPolicy, ...] = tuple(
            build("policy", spec) for spec in config.workflow.after_iteration
        )
        self.retention: RetentionPolicy = build("retention", config.evolution.retention)
        self.parent_sampler: ParentSampler = build("sampler", config.evolution.parent_sampler)
        self.attempt_history: list[dict[str, Any]] = []
        self.usage_records: list[UsageRecord] = []
        self.usage_entries: list[dict[str, Any]] = []
        self.candidates: dict[str, CandidateRecord] = {}
        self.populations: list[list[CandidateRecord]] = []
        self.artifacts: dict[str, object] = {}
        self.events: EventWriter | None = None
        self.run_id: str | None = None
        self.trace_ledger: TraceLedger | None = None
        self.data: DataPlan | None = None
        self.evaluator: Evaluator | None = None
        self.reference: Path | None = None
        self.champion: tuple[int, CandidateRecord] | None = None
        self.final_result: FinalistResult | None = None
        self.final_solver: Path | None = None
        self.result: EvolveResult | None = None
        self._render_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._refresh_stop = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._refresh_poll_seconds = 2.0
        self._refresh_due_seconds = 4.0
        self.direction_assignments: dict[int, dict[str, object]] = {}
        self.variant_handles: dict[str, VariantHandle] = {}
        self.variant_bases: dict[str, Path] = {}
        self.variant_public: dict[str, EvaluationResult] = {}
        self.variant_validation: dict[str, EvaluationResult] = {}
        self.research_finalists: dict[str, CandidateRecord] = {}
        self.validation_query_count = 0
        self.validation_selection_count = 0
        self._last_status_refresh = 0.0
        self._cancellation_event = threading.Event()
        self.attempt_runner = AttemptRunner(self)
        self.role_jobs = RoleJobs(self)
        service_impl = EngineControllerServices(self)
        self.controller_services = ControllerServices(
            run_trusted_agent=self.role_jobs.run,
            materialize_variant=service_impl.materialize_variant,
            evaluate_public_variant=service_impl.evaluate_public_variant,
            select_by_validation=service_impl.select_by_validation,
            register_finalist=service_impl.register_finalist,
        )
        self.integrity_executor = IntegrityReviewExecutor(
            run_dir=self.run_dir, interface=self.interface, editable=self.editable,
            workers=config.workers, reviewer_config=config.reviewer_config,
            run_agent=self.controller_services.run_trusted_agent, emit=self._emit,
        )

    def _population_snapshot(self) -> tuple[tuple[CandidateRecord, ...], ...]:
        return tuple(tuple(population) for population in self.populations)

    def _set_directions(self, directions: Mapping[int, dict[str, object]]) -> None:
        self.direction_assignments = dict(directions)

    def _llm_client(self, request: WorkerRequest) -> LLMClient:
        if self.trace_ledger is None:
            raise RuntimeError("Trace ledger is not initialized.")
        return LLMClient(
            adapter=self.worker_adapter,
            request=request,
            ledger=self.trace_ledger,
            emit=self._emit,
            record_usage=self._record_worker_usage,
            generation_guidance=lambda: build_generation_guidance(
                self.config.assessment, self.config.execution.feedback_mode
            ),
        )

    def _record_worker_usage(self, request: WorkerRequest, outcome: WorkerOutcome) -> None:
        entry = {
            "worker": f"{request.worker.harness}:{request.worker.model}",
            "trace": dict(request.trace_context.get("join", {})),
            "usage": outcome.usage.as_dict(),
        }
        with self._usage_lock:
            self.usage_records.append(outcome.usage)
            self.usage_entries.append(entry)
            _write_json(
                self.run_dir / "controller" / "usage.json",
                {
                    "schema": "usage_aggregate/1",
                    "aggregate": aggregate_usage(self.usage_records).as_dict(),
                    "records": list(self.usage_entries),
                },
            )

    def _raise_if_cancelled(self) -> None:
        if self._cancellation_event.is_set():
            raise _RunCancelled("Evolution run cancelled by controller signal.")


    def run(self) -> EvolveResult:
        self._initialize_run_directory()
        self._cancellation_event.clear()
        self.run_id = secrets.token_hex(16)
        self.events = EventWriter(self.run_dir / "events.jsonl")
        run_succeeded = False
        try:
            with _controller_cancellation(self._cancellation_event):
                self._refresh_stop.clear()
                self._refresh_thread = threading.Thread(
                    target=self._refresh_status_loop, name="ope-status-refresher", daemon=True
                )
                self._refresh_thread.start()
                reviewer_worker = (
                    self.config.reviewer_config().resolved_worker(self.config.workers)
                    if self.config.assessment.enabled("integrity", "validation") else None
                )
                provenance_workers = self.config.workers.pool
                if reviewer_worker is not None and reviewer_worker not in provenance_workers:
                    provenance_workers = (*provenance_workers, reviewer_worker)
                provenance = build_run_provenance(
                    self.config,
                    run_id=self.run_id,
                    worker_adapter=self.worker_adapter,
                    worker_provenance=self.worker_adapter.provenance(provenance_workers),
                    evaluator_factory=self.evaluator_factory,
                )
                self.trace_ledger = TraceLedger(
                    self.run_dir,
                    run_id=self.run_id,
                    config_hash=str(provenance["config_hash"]),
                    adapter_name=self.worker_adapter.name,
                )
                _write_json(self.run_dir / "resolved_config.json", self.config.redacted_dict())
                _write_json(self.run_dir / "provenance.json", provenance)
                self._emit("run_started", "running", data=provenance)
                self._run_phases()
                self._raise_if_cancelled()
                if self.result is None:
                    raise RuntimeError("Workflow completed without producing EvolveResult.")
                self._emit(
                    "run_finished",
                    "succeeded",
                    data={"best_candidate_id": self.result.best_candidate_id},
                )
                run_succeeded = True
                return self.result
        except _RunCancelled as exc:
            self._emit("run_finished", "cancelled", data={"error": str(exc)})
            raise
        except Exception as exc:
            self._emit(
                "run_finished",
                "failed",
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        finally:
            trace_failure: Exception | None = None
            try:
                if self.trace_ledger is None:
                    raise RuntimeError("Trace ledger was not initialized.")
                coverage = self.trace_ledger.recover_and_summarize()
                capture = coverage["capture_quality"]
                outcomes = coverage["outcomes"]
                gateway = coverage["provider_gateway"]
                if not all(isinstance(value, Mapping) for value in (capture, outcomes, gateway)):
                    raise ValueError("Trace coverage has an invalid shape.")
                self._emit(
                    "trace_coverage",
                    "succeeded",
                    data={
                        "total": coverage["total"],
                        **{f"capture_{key}": value for key, value in capture.items()},
                        **{f"outcome_{key}": value for key, value in outcomes.items()},
                        **{f"gateway_{key}": value for key, value in gateway.items()},
                    },
                )
                append_trace_coverage(self.run_dir, coverage)
            except Exception as exc:
                trace_failure = exc
                try:
                    self._emit("trace_coverage", "failed")
                except Exception:
                    pass
            if self.config.sandbox.backend == "docker":
                cleanup_errors: tuple[str, ...]
                try:
                    cleanup_errors = cleanup_managed_resources(
                        run_id=self.run_id,
                        include_active=True,
                    )
                except Exception as exc:
                    cleanup_errors = (f"{type(exc).__name__}: {exc}",)
                try:
                    _write_json(
                        self.run_dir / "controller" / "docker_gc.json",
                        {
                            "schema": "docker_gc_result/1",
                            "complete": not cleanup_errors,
                            "errors": cleanup_errors,
                        },
                    )
                    self._emit(
                        "docker_gc_finished",
                        "failed" if cleanup_errors else "succeeded",
                    )
                except Exception:
                    pass
            self._refresh_stop.set()
            if self._refresh_thread is not None and self._refresh_thread.ident is not None:
                self._refresh_thread.join(timeout=10)
                self._refresh_thread = None
            try:
                if self.events is not None:
                    self._refresh_status(include_report=True)
            finally:
                if self.events is not None:
                    self.events.close()
                    self.events = None
            if trace_failure is not None and run_succeeded:
                raise RuntimeError(
                    "Trace finalization failed after a successful run."
                ) from trace_failure

    def _run_phases(self) -> None:
        for spec, phase in zip(self.config.workflow.phases, self.phases, strict=True):
            self._run_phase(phase, spec.options)

    def _run_phase(self, phase: Phase, options: Mapping[str, object]) -> Mapping[str, object]:
        self._raise_if_cancelled()
        missing = phase.requires.difference(self.artifacts)
        if missing:
            raise RuntimeError(
                f"Phase {phase.name!r} requires missing artifacts {sorted(missing)!r}."
            )
        scope = {"phase": phase.name}
        self._emit("phase_started", "running", scope=scope)
        self._refresh_status()
        try:
            context = PhaseContext(
                run_dir=self.run_dir,
                options=options,
                artifacts=MappingProxyType(dict(self.artifacts)),
                emit=self._scoped_emitter(scope),
                services=self.controller_services,
            )
            result = phase.run(bind_context(self, phase.name, context))
            undeclared = set(result.artifacts).difference(phase.provides)
            if undeclared:
                raise RuntimeError(
                    f"Phase {phase.name!r} produced undeclared artifacts {sorted(undeclared)!r}."
                )
            commit_result(self, result)
            self.artifacts.update(result.artifacts)
            self._emit(
                "phase_finished",
                result.status,
                scope=scope,
                data={"artifacts": sorted(result.artifacts)},
            )
        except _RunCancelled:
            self._emit("phase_finished", "cancelled", scope=scope)
            raise
        except Exception as exc:
            self._emit(
                "phase_finished",
                "failed",
                scope=scope,
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        finally:
            self._refresh_status()
        self._raise_if_cancelled()
        return result.artifacts

    def _accept_and_record(self, attempt: Attempt) -> None:
        record = attempt.record
        accepted = False
        worker_exit_warning = _worker_exit_warning(attempt.worker_outcome)
        if record.valid:
            population = [*self.populations[record.island], record]
            try:
                retained = self._trim_population(population)
            except MetricSelectionError as exc:
                record = replace(
                    record,
                    valid=False,
                    error=f"metric_incomplete: {exc}",
                )
            else:
                self.populations[record.island] = retained
                accepted = record.candidate_id in {item.candidate_id for item in retained}
        self.candidates[record.candidate_id] = record
        summary = {
            "attempt_id": record.attempt_id,
            "candidate_id": record.candidate_id,
            "island": record.island,
            "iteration": record.iteration,
            "attempt_index": record.attempt_index,
            "parent_id": record.parent_id,
            "guidance": record.guidance,
            "public_score": record.public_score if attempt.evaluated else None,
            "validation_score": record.validation_score,
            "assessment_score": record.assessment_score,
            "assessment": record.assessment,
            "review_verdict": record.review_verdict,
            "review_report": record.review_report,
            "valid": record.valid,
            "error": record.error,
            "changed_files": list(attempt.changed),
            "worker_returncode": attempt.worker_outcome.returncode,
            "worker_timed_out": attempt.worker_outcome.timed_out,
            "worker_cancelled": attempt.worker_outcome.cancelled,
            "worker_termination_reason": attempt.worker_outcome.termination_reason,
            "worker_exit_warning": worker_exit_warning,
            "transcript": str(attempt.worker_outcome.transcript),
            "native_trace": str(attempt.worker_outcome.native_trace)
            if attempt.worker_outcome.native_trace
            else None,
            "stderr_trace": str(attempt.worker_outcome.stderr_trace)
            if attempt.worker_outcome.stderr_trace
            else None,
            "trace_chunks": str(attempt.worker_outcome.trace_chunks)
            if attempt.worker_outcome.trace_chunks
            else None,
            "trace_outcome": str(attempt.worker_outcome.trace_outcome)
            if attempt.worker_outcome.trace_outcome
            else None,
            "trace_capture_error": attempt.worker_outcome.capture_error,
            "solver_explanation": (
                str(attempt.worker_outcome.solver_explanation)
                if attempt.worker_outcome.solver_explanation
                else None
            ),
            "solver_explanation_status": (
                explanation_status(attempt.worker_outcome.solver_explanation)
                if attempt.worker_outcome.solver_explanation
                else "missing"
            ),
            "solver_explanation_summary": (
                explanation_memory(attempt.worker_outcome.solver_explanation)
                if attempt.worker_outcome.solver_explanation
                else None
            ),
            "usage": attempt.worker_outcome.usage.as_dict(),
        }
        self.attempt_history.append(summary)
        _append_json(self.run_dir / "attempts.jsonl", summary)
        self._emit(
            "attempt_finished",
            "succeeded" if record.valid else "failed",
            scope={
                "phase": "explore",
                "iteration": record.iteration,
                "island": record.island,
                "attempt_id": record.attempt_id,
            },
            data={**summary, "accepted": accepted},
        )

    def _apply_population_edit(self, edit: PopulationEdit) -> None:
        if edit.budget:
            raise NotImplementedError(
                "PopulationEdit.budget is reserved for a future scheduler and cannot "
                "be used until the engine has an explicit budget consumer."
            )
        killed = set(edit.kill)
        populations = [
            [record for record in population if record.candidate_id not in killed]
            for population in self.populations
        ]
        for candidate_id, target in edit.migrate:
            if not 0 <= target < len(populations):
                raise ValueError(f"Migration target island is out of range: {target}")
            try:
                record = self.candidates[candidate_id]
            except KeyError as exc:
                raise ValueError(
                    f"Migration references unknown candidate {candidate_id!r}."
                ) from exc
            if not record.valid or record.assessment_score is None:
                raise ValueError(f"Migration candidate {candidate_id!r} has not passed admission.")
            if candidate_id in killed:
                raise ValueError(
                    f"Migration candidate {candidate_id!r} is also marked for removal."
                )
            populations[target] = self._trim_population([*populations[target], record])
        self.populations = populations

    def _validate_candidate(self, root: Path) -> None:
        validate_tree_safety(
            root,
            max_files=self.config.sandbox.max_candidate_files,
            max_bytes=self.config.sandbox.max_candidate_bytes,
        )
        validate_interface(root, self.interface, self.runtime)
        validate_candidate_imports(
            root,
            runtime=self.runtime,
            forbidden=self.config.execution.forbidden_candidate_imports,
        )



    def _select_parent(
        self, population: list[CandidateRecord], rng: random.Random
    ) -> CandidateRecord:
        ordered = self._trim_population(population)
        selected = self.parent_sampler.select(tuple(ordered), rng)
        allowed = {record.candidate_id: record for record in ordered}
        if selected.candidate_id not in allowed:
            raise RuntimeError("Parent sampler returned a candidate outside the island archive.")
        return allowed[selected.candidate_id]

    def _select_worker(self, rng: random.Random) -> WorkerConfig:
        pool = self.config.workers.pool
        ticket = rng.randrange(sum(worker.weight for worker in pool))
        for worker in pool:
            ticket -= worker.weight
            if ticket < 0:
                return worker
        raise RuntimeError("Worker pool has no positive weight.")

    def _trim_population(self, population: list[CandidateRecord]) -> list[CandidateRecord]:
        capacity = self.config.evolution.population_per_island
        available = {record.candidate_id: record for record in population}
        retained = self.retention.retain(tuple(population), capacity)
        ids = [record.candidate_id for record in retained]
        if len(ids) != len(set(ids)):
            raise RuntimeError("Retention policy returned duplicate candidate ids.")
        if len(ids) > capacity:
            raise RuntimeError("Retention policy exceeded the island archive capacity.")
        unknown = sorted(set(ids).difference(available))
        if unknown:
            raise RuntimeError(f"Retention policy returned unknown candidates: {unknown!r}")
        return [available[candidate_id] for candidate_id in ids]

    def _fixed_finalists(
        self, populations: list[list[CandidateRecord]]
    ) -> list[tuple[int, CandidateRecord]]:
        fixed: list[tuple[int, CandidateRecord]] = []
        seen: set[str] = set()
        count = self.config.evolution.finalists_per_island
        for island, population in enumerate(populations):
            for record in self._trim_population(population)[:count]:
                if record.candidate_id not in seen:
                    fixed.append((island, record))
                    seen.add(record.candidate_id)
        return fixed

    def _memory_for(self, island: int) -> str | None:
        mode = self.config.workers.tools.communication
        if mode == "none" or not self.attempt_history:
            return None
        history = self.attempt_history
        if mode == "island":
            history = [item for item in history if item["island"] == island]
        if mode == "controller_summary":
            ranked = sorted(
                history,
                key=lambda item: (
                    float(item["public_score"])
                    if isinstance(item.get("public_score"), (int, float))
                    else float("-inf")
                ),
                reverse=True,
            )[:5]
            recent = history[-2:]
            history = list({item["attempt_id"]: item for item in [*ranked, *recent]}.values())
        else:
            history = history[-10:]
        allowed = {
            "attempt_id",
            "candidate_id",
            "island",
            "iteration",
            "attempt_index",
            "parent_id",
            "public_score",
            "changed_files",
            "worker_returncode",
            "worker_timed_out",
            "solver_explanation_summary",
        }
        worker_visible = [
            {key: value for key, value in item.items() if key in allowed} for item in history
        ]
        return json.dumps(worker_visible, indent=2, sort_keys=True)

    def _initialize_run_directory(self) -> None:
        if self.initial.is_dir():
            try:
                self.run_dir.relative_to(self.initial)
            except ValueError:
                pass
            else:
                raise ValueError("run_dir cannot be inside the initial solver directory.")
        if self.run_dir.exists() and any(self.run_dir.iterdir()):
            raise FileExistsError(f"run_dir must be empty or absent: {self.run_dir}")
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def _write_checkpoint(self, iteration: int) -> None:
        payload = {
            "iteration": iteration,
            "populations": [
                [record.candidate_id for record in population] for population in self.populations
            ],
            "candidates": {key: value.as_dict() for key, value in sorted(self.candidates.items())},
        }
        _write_json(self.run_dir / "checkpoints" / f"iteration_{iteration:03d}.json", payload)
        _write_json(self.run_dir / "state.json", payload)

    def _emit(
        self,
        kind: str,
        status: str,
        *,
        scope: Mapping[str, object] | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        if self.events is None:
            return
        self.events.emit(kind, status, scope=scope, data=data)

    def _scoped_emitter(self, scope: Mapping[str, object]):
        def emit(kind: str, status: str, data: Mapping[str, object] | None = None) -> None:
            self._emit(kind, status, scope=scope, data=data)

        return emit

    def _refresh_status_loop(self) -> None:
        # Events stream into the ledger continuously, but the main thread
        # only refreshes at attempt and phase boundaries; during a long
        # agent invocation the on-disk dashboards would otherwise go stale
        # for the whole invocation. The ledger stays the single source of
        # truth; this thread merely re-renders projections of it.
        while not self._refresh_stop.wait(self._refresh_poll_seconds):
            try:
                self._refresh_status_if_due(interval_seconds=self._refresh_due_seconds)
            except Exception:
                continue

    def _refresh_status(self, *, include_report: bool = False) -> None:
        if self.events is None:
            return
        with self._render_lock:
            if self.events is None:
                return
            self.events.flush()
            try:
                public_events = self.run_dir / "public_events.jsonl"
                project_public_events(self.run_dir / "events.jsonl", public_events)
                state = render_status(public_events, self.run_dir / "public_status.html")
                render_public_report(state, self.run_dir / "PUBLIC_REPORT.md")
                if include_report:
                    render_final_report(public_events, self.run_dir / "report.html")
                materialize_public_bundle(self.run_dir)
                render_owner_views(
                    self.run_dir / "events.jsonl",
                    self.run_dir,
                    final=include_report,
                )
            except Exception as exc:
                self._emit(
                    "public_view_refresh_failed",
                    "failed",
                    data={"error": f"{type(exc).__name__}: {exc}"},
                )
            finally:
                self._last_status_refresh = time.monotonic()

    def _refresh_status_if_due(self, interval_seconds: float = 1.0) -> None:
        if time.monotonic() - self._last_status_refresh >= interval_seconds:
            self._refresh_status()


@contextmanager
def _controller_cancellation(event: threading.Event) -> Iterator[None]:
    """Translate SIGINT/SIGTERM into a cooperative controller cancellation."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    watched = tuple(
        item
        for item in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None))
        if item is not None
    )
    previous = {item: signal.getsignal(item) for item in watched}

    def request_cancellation(_signum: int, _frame: object) -> None:
        event.set()

    try:
        for item in watched:
            signal.signal(item, request_cancellation)
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


__all__: list[str] = []
