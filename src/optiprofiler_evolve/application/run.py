"""Compose one run and manage its infrastructure lifecycle."""

from __future__ import annotations

import json
import logging
import random
import secrets
import signal
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from optiprofiler_evolve.agents.contracts import WorkerAdapter, WorkerOutcome, WorkerRequest
from optiprofiler_evolve.agents.docker_runtime import cleanup_managed_resources
from optiprofiler_evolve.agents.llm import LLMClient
from optiprofiler_evolve.config import EvolveConfig, WorkerConfig
from optiprofiler_evolve.controller.builtins import register_builtin_components
from optiprofiler_evolve.controller.commits import RunCommits
from optiprofiler_evolve.controller.contracts import ControllerServices
from optiprofiler_evolve.controller.coordinator import RunCoordinator
from optiprofiler_evolve.controller.finalists import FinalistRegistrar
from optiprofiler_evolve.controller.phase_contexts import context_factories
from optiprofiler_evolve.controller.registry import build, resolve
from optiprofiler_evolve.controller.research_evaluation import ResearchEvaluator
from optiprofiler_evolve.controller.role_jobs import RoleJobs
from optiprofiler_evolve.controller.runtime import RunEnvironment
from optiprofiler_evolve.controller.selection import (
    ParentSampler,
    RetentionPolicy,
)
from optiprofiler_evolve.controller.state import RunState
from optiprofiler_evolve.controller.validation import ValidationSelector
from optiprofiler_evolve.controller.variants import VariantMaterializer
from optiprofiler_evolve.criteria.guidance import build_generation_guidance
from optiprofiler_evolve.criteria.integrity.execution import IntegrityReviewExecutor
from optiprofiler_evolve.criteria.performance.contracts import Evaluator
from optiprofiler_evolve.errors import RunCancelled as _RunCancelled
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import (
    EvolveResult,
)
from optiprofiler_evolve.observability.events import EventWriter
from optiprofiler_evolve.observability.live_views import LiveViews
from optiprofiler_evolve.observability.provenance import build_run_provenance
from optiprofiler_evolve.observability.reports import append_trace_coverage
from optiprofiler_evolve.observability.trace_ledger import TraceLedger
from optiprofiler_evolve.observability.usage import UsageRecord, aggregate_usage
from optiprofiler_evolve.phases.attempts.contracts import (
    AfterIterationPolicy,
    AttemptStep,
)
from optiprofiler_evolve.phases.attempts.runner import AttemptRunner
from optiprofiler_evolve.phases.contracts import Phase
from optiprofiler_evolve.solver import (
    InterfaceSpec,
    validate_candidate_imports,
    validate_interface,
    validate_tree_safety,
)


class EvolutionRun:
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
        self.usage_records: list[UsageRecord] = []
        self.usage_entries: list[dict[str, Any]] = []
        self.events: EventWriter | None = None
        self.run_id: str | None = None
        self.trace_ledger: TraceLedger | None = None
        self.views = LiveViews(self.run_dir, self._emit)
        self.state = RunState()
        self.commits = RunCommits(
            self.state, config, self.run_dir, self.retention, self.parent_sampler, self._emit
        )
        self._usage_lock = threading.Lock()
        self._cancellation_event = threading.Event()
        self.environment = RunEnvironment(
            initial=self.initial,
            interface=self.interface,
            runtime=self.runtime,
            editable=self.editable,
            config=config,
            run_dir=self.run_dir,
            state=self.state,
            commits=self.commits,
            attempt_steps=self.attempt_steps,
            evaluator_factory=self.evaluator_factory,
            emit=self._emit,
            scoped_emitter=self._scoped_emitter,
            validate_candidate=self._validate_candidate,
            memory_for=self._memory_for,
            llm_client=self._llm_client,
            cancellation_event=self._cancellation_event,
        )
        self.attempt_runner = AttemptRunner(self.environment)
        self.role_jobs = RoleJobs(self.environment)
        self.controller_services = ControllerServices(
            run_trusted_agent=self.role_jobs.run,
            materialize_variant=VariantMaterializer(self.environment).materialize,
            evaluate_public_variant=ResearchEvaluator(self.environment).evaluate,
            select_by_validation=ValidationSelector(self.environment).select,
            register_finalist=FinalistRegistrar(self.environment).register,
        )
        self.integrity_executor = IntegrityReviewExecutor(
            run_dir=self.run_dir,
            interface=self.interface,
            editable=self.editable,
            workers=config.workers,
            reviewer_config=config.reviewer_config,
            run_agent=self.controller_services.run_trusted_agent,
            emit=self._emit,
        )

        self.coordinator = RunCoordinator(
            state=self.state,
            phases=tuple(zip(config.workflow.phases, self.phases, strict=True)),
            run_dir=self.run_dir,
            services=self.controller_services,
            commits=self.commits,
            contexts=context_factories(
                self.environment,
                review_candidate=self.integrity_executor.review,
                run_attempt=self.attempt_runner.run,
                failed_attempt=self.attempt_runner.failed,
                select_worker=self._select_worker,
                after_iteration=self.after_iteration,
                refresh=self.views.refresh,
                refresh_if_due=self.views.refresh_if_due,
                raise_if_cancelled=self._raise_if_cancelled,
            ),
            emit=self._emit,
            refresh=self.views.refresh,
            raise_if_cancelled=self._raise_if_cancelled,
        )

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
                self.views.start(self.events)
                reviewer_worker = (
                    self.config.reviewer_config().resolved_worker(self.config.workers)
                    if self.config.assessment.enabled("integrity", "validation")
                    else None
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
                self.coordinator.run()
                self._raise_if_cancelled()
                if self.state.result is None:
                    raise RuntimeError("Workflow completed without producing EvolveResult.")
                self._emit(
                    "run_finished",
                    "succeeded",
                    data={"best_candidate_id": self.state.result.best_candidate_id},
                )
                run_succeeded = True
                return self.state.result
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
                    logging.getLogger(__name__).exception(
                        "Failed to record trace finalization failure"
                    )
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
                    logging.getLogger(__name__).exception(
                        "Failed to record Docker cleanup evidence"
                    )
            self.views.stop()
            try:
                if self.events is not None:
                    self.views.refresh(include_report=True)
            finally:
                if self.events is not None:
                    self.events.close()
                    self.events = None
                    self.views.events = None
            if trace_failure is not None and run_succeeded:
                raise RuntimeError(
                    "Trace finalization failed after a successful run."
                ) from trace_failure

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

    def _select_worker(self, rng: random.Random) -> WorkerConfig:
        pool = self.config.workers.pool
        ticket = rng.randrange(sum(worker.weight for worker in pool))
        for worker in pool:
            ticket -= worker.weight
            if ticket < 0:
                return worker
        raise RuntimeError("Worker pool has no positive weight.")

    def _memory_for(self, island: int) -> str | None:
        mode = self.config.workers.tools.communication
        if mode == "none" or not self.state.attempt_history:
            return None
        history = self.state.attempt_history
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
