"""Deterministic controller for benchmark-driven solver evolution."""

from __future__ import annotations

import json
import os
import random
import secrets
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

from optiprofiler_evolve.agents.docker_runtime import cleanup_managed_resources
from optiprofiler_evolve.agents.llm import LLMClient
from optiprofiler_evolve.controller.attempts import AttemptRunner
from optiprofiler_evolve.shared.models import Attempt
from optiprofiler_evolve.controller.builtins import register_builtin_components
from optiprofiler_evolve.controller.phase_contexts import bind_context, commit_result
from optiprofiler_evolve.controller.registry import build, resolve
from optiprofiler_evolve.controller.role_jobs import RoleJobs
from optiprofiler_evolve.controller.selection import MetricSelectionError
from optiprofiler_evolve.controller.services import EngineControllerServices
from optiprofiler_evolve.observability.events import EventWriter
from optiprofiler_evolve.observability.projections import project_public_events
from optiprofiler_evolve.observability.provenance import build_run_provenance
from optiprofiler_evolve.observability.trace_ledger import TraceLedger
from optiprofiler_evolve.observability.usage import UsageRecord, aggregate_usage
from optiprofiler_evolve.phases.report.owner_views import render_owner_views
from optiprofiler_evolve.phases.report.reports import append_trace_coverage
from optiprofiler_evolve.phases.report.viewers import (
    materialize_public_bundle,
    render_final_report,
    render_public_report,
    render_status,
)
from optiprofiler_evolve.quality.complexity import (
    ComplexityChecker,
    ComplexityLimitExceeded,
    ComplexityReport,
)
from optiprofiler_evolve.quality.review import (
    CandidateReviewer,
    IntegrityReviewDecision,
    IntegrityReviewRequest,
    write_private_review,
    write_sanitized_transcript,
)
from optiprofiler_evolve.shared.agent_helpers import worker_exit_warning as _worker_exit_warning
from optiprofiler_evolve.shared.config import EvolveConfig, WorkerConfig
from optiprofiler_evolve.shared.data import DataPlan
from optiprofiler_evolve.shared.errors import (
    IntegrityReviewerUnavailable as _IntegrityReviewerUnavailable,
)
from optiprofiler_evolve.shared.errors import RunCancelled as _RunCancelled
from optiprofiler_evolve.shared.explanations import (
    explanation_memory,
    explanation_status,
)
from optiprofiler_evolve.shared.files import append_json as _append_json
from optiprofiler_evolve.shared.files import write_json as _write_json
from optiprofiler_evolve.shared.models import (
    CandidateRecord,
    EvaluationResult,
    EvolveResult,
    FinalistResult,
)
from optiprofiler_evolve.shared.protocols import (
    AfterIterationPolicy,
    AttemptStep,
    ControllerServices,
    Evaluator,
    ParentSampler,
    Phase,
    PhaseContext,
    PopulationEdit,
    RetentionPolicy,
    VariantHandle,
    WorkerAdapter,
    WorkerOutcome,
    WorkerRequest,
)
from optiprofiler_evolve.shared.solver import (
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
        self.evaluator_factory = evaluator_factory or resolve(
            "evaluator", config.evaluation.adapter
        )
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
        self.integrity_reviewer: CandidateReviewer = build(
            "reviewer", config.integrity_review.component
        )
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
        self.complexity: ComplexityChecker | None = None
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
        self.integrity_reviews: dict[str, IntegrityReviewDecision] = {}
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

    def _review_candidate(
        self,
        *,
        candidate_id: str,
        candidate: Path,
        parent: Path,
        changed: tuple[str, ...],
        mutation_transcript: Path | None,
        mutation_worker: WorkerConfig | None,
    ) -> tuple[IntegrityReviewDecision, Path]:
        """Run the mandatory semantic gate before any validation query."""

        cached = self.integrity_reviews.get(candidate_id)
        normalized_root = self.run_dir / "controller" / "integrity_reviews" / candidate_id
        if cached is not None:
            return cached, normalized_root / "decision.json"

        transcript = normalized_root / "inputs" / "mutation_transcript.txt"
        secret_values: dict[str, str] = {}
        if mutation_worker is not None:
            secret_values.update(mutation_worker.env)
            secret_values.update(
                {name: os.environ[name] for name in mutation_worker.pass_env if name in os.environ}
            )
        if mutation_transcript is None:
            mutation_transcript = normalized_root / "inputs" / "empty_transcript.txt"
            mutation_transcript.parent.mkdir(parents=True, exist_ok=True)
            mutation_transcript.write_text(
                "[controller] no mutation transcript exists for this materialized variant.\n",
                encoding="utf-8",
            )
            mutation_transcript.chmod(0o600)
        write_sanitized_transcript(
            mutation_transcript,
            transcript,
            secret_values=secret_values,
        )
        config = self.config.integrity_review
        reviewer_worker = config.resolved_worker(self.config.workers)
        failures: list[str] = []
        review_scope = {
            "phase": "explore",
            "attempt_id": candidate_id,
            "role": "integrity-reviewer",
        }
        self._emit("integrity_review_started", "running", scope=review_scope)
        for review_attempt in range(1, config.retries + 2):
            scope = {
                **review_scope,
                "job_id": f"{candidate_id}-r{review_attempt:02d}",
            }
            self._emit(
                "integrity_review_attempt_started",
                "running",
                scope=scope,
                data={"review_attempt": review_attempt},
            )
            try:
                decision = self.integrity_reviewer.review(
                    IntegrityReviewRequest(
                        candidate_id=candidate_id,
                        candidate=candidate,
                        parent=parent,
                        changed_files=changed,
                        interface=f"{self.interface.file}:{self.interface.function}",
                        editable=self.editable,
                        mutation_transcript=transcript,
                        reviewer_worker=reviewer_worker,
                        review_attempt=review_attempt,
                        timeout_seconds=config.timeout_seconds,
                        token_budget=config.token_budget,
                        max_budget_usd=config.max_budget_usd,
                        run_agent=self.controller_services.run_trusted_agent,
                    )
                )
            except _RunCancelled:
                raise
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                failures.append(failure)
                self._emit(
                    "integrity_review_attempt_finished",
                    "failed",
                    scope=scope,
                    data={
                        "gate": "unavailable",
                        "review_attempt": review_attempt,
                        "error": failure,
                    },
                )
                continue
            attempt_report = normalized_root / f"attempt_{review_attempt:02d}.json"
            write_private_review(attempt_report, decision)
            write_private_review(normalized_root / "decision.json", decision)
            self.integrity_reviews[candidate_id] = decision
            self._emit(
                "integrity_review_attempt_finished",
                "succeeded",
                scope=scope,
                data={
                    "review_attempt": review_attempt,
                    "verdict": decision.verdict,
                    "checked": list(decision.checked),
                    "finding_count": len(decision.findings),
                    "report": str(attempt_report),
                },
            )
            self._emit(
                "integrity_review_finished",
                "succeeded",
                scope=review_scope,
                data={"gate": "approved" if decision.verdict == "approve" else "quarantined"},
            )
            return decision, normalized_root / "decision.json"

        decision = IntegrityReviewDecision(
            verdict="quarantine",
            checked=(),
            summary="Integrity reviewer was unavailable after all configured attempts.",
            reason="reviewer_unavailable",
        )
        report = normalized_root / "decision.json"
        write_private_review(report, decision)
        self.integrity_reviews[candidate_id] = decision
        self._emit(
            "integrity_review_finished",
            "failed",
            scope=review_scope,
            data={"gate": "unavailable" if config.strict else "quarantined"},
        )
        if config.strict:
            raise _IntegrityReviewerUnavailable(
                "Integrity reviewer was unavailable in strict mode: " + "; ".join(failures)
            )
        return decision, report

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
                reviewer_worker = self.config.integrity_review.resolved_worker(self.config.workers)
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
            if not record.valid or (candidate_id != "seed" and record.review_verdict != "approve"):
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
            forbidden=self.config.evaluation.forbidden_candidate_imports,
        )

    def _assess_complexity(
        self,
        root: Path,
        scope: Mapping[str, object] | None = None,
    ) -> ComplexityReport | None:
        if self.complexity is None:
            return None
        self._validate_candidate(root)
        report = self.complexity.assess(root)
        self._emit(
            "complexity_checked",
            "failed" if report.blocked else "succeeded",
            scope=scope,
            data=report.summary(),
        )
        return report

    def _check_complexity(
        self,
        root: Path,
        scope: Mapping[str, object] | None = None,
    ) -> ComplexityReport | None:
        report = self._assess_complexity(root, scope)
        if report is not None and report.blocked:
            raise ComplexityLimitExceeded(report)
        return report

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
