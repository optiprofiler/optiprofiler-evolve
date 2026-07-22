"""Deterministic controller for benchmark-driven solver evolution."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import random
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .broker import BrokerConnection, EvaluationBroker
from .builtins import register_builtin_components
from .config import EvolveConfig, ToolConfig, WorkerConfig
from .data import DataPlan, resolve_data_plan, write_data_manifests
from .events import EventWriter
from .models import CandidateRecord, EvaluationResult, EvolveResult, FinalistResult
from .prompt import build_worker_prompt
from .protocols import (
    AfterIterationPolicy,
    AgentJob,
    AgentJobResult,
    AttemptCapabilities,
    AttemptContext,
    AttemptStep,
    ControllerServices,
    Evaluator,
    IterationView,
    Phase,
    PhaseContext,
    PopulationEdit,
    StepResult,
    WorkerAdapter,
    WorkerOutcome,
    WorkerRequest,
    VariantHandle,
    VariantRequest,
)
from .provenance import build_run_provenance, coordinate_seed
from .projections import project_public_events
from .references import materialize_reference
from .research import file_hash, read_json_object, safe_relative_path
from .registry import build, resolve
from .sandbox import AgentRunResult
from .solver import (
    InterfaceSpec,
    changed_files,
    copy_initial_source,
    copy_solver_tree,
    tree_hash,
    validate_candidate_imports,
    validate_edit_scope,
    validate_interface,
    validate_tree_safety,
)
from .viewers import render_final_report, render_status


AgentRunner = Callable[..., AgentRunResult]


@dataclass(frozen=True)
class _Attempt:
    record: CandidateRecord
    changed: tuple[str, ...]
    worker_outcome: WorkerOutcome
    step_results: tuple[StepResult, ...]


class _AgentRunnerAdapter:
    """Compatibility adapter for tests and owner-supplied legacy runners."""

    name = "injected-runner"

    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    def run(self, request: WorkerRequest) -> WorkerOutcome:
        result = self.runner(
            worker=request.worker,
            workers=request.workers,
            sandbox=request.sandbox,
            workspace=request.workspace,
            tools_dir=request.tools_dir,
            broker=request.broker,
            prompt=request.prompt,
            transcript=request.transcript,
        )
        return WorkerOutcome(
            result.returncode,
            result.transcript,
            result.timed_out,
            native_trace=result.native_trace,
            stderr_trace=result.stderr_trace,
            trace_chunks=result.trace_chunks,
            capture_error=result.capture_error,
        )

    def provenance(self, workers: Sequence[WorkerConfig]) -> Mapping[str, object]:
        module = getattr(self.runner, "__module__", self.runner.__class__.__module__)
        qualname = getattr(self.runner, "__qualname__", self.runner.__class__.__qualname__)
        return {
            "adapter": self.name,
            "runner": f"{module}:{qualname}",
            "models": sorted({worker.model for worker in workers}),
        }


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
        agent_runner: AgentRunner | None = None,
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
        if worker_adapter is not None and agent_runner is not None:
            raise ValueError("Provide worker_adapter or agent_runner, not both.")
        if worker_adapter is not None:
            self.worker_adapter = worker_adapter
        elif agent_runner is not None:
            self.worker_adapter = _AgentRunnerAdapter(agent_runner)
        else:
            self.worker_adapter = resolve("worker", config.workers.adapter)()
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
        self.attempt_history: list[dict[str, Any]] = []
        self.candidates: dict[str, CandidateRecord] = {}
        self.populations: list[list[CandidateRecord]] = []
        self.artifacts: dict[str, object] = {}
        self.events: EventWriter | None = None
        self.data: DataPlan | None = None
        self.evaluator: Evaluator | None = None
        self.reference: Path | None = None
        self.champion: tuple[int, CandidateRecord] | None = None
        self.final_result: FinalistResult | None = None
        self.final_solver: Path | None = None
        self.result: EvolveResult | None = None
        self.direction_assignments: dict[int, dict[str, object]] = {}
        self.variant_handles: dict[str, VariantHandle] = {}
        self.variant_public: dict[str, EvaluationResult] = {}
        self.variant_validation: dict[str, EvaluationResult] = {}
        self.research_finalists: dict[str, CandidateRecord] = {}
        self.validation_query_count = 0
        self.validation_selection_count = 0
        service_impl = _EngineControllerServices(self)
        self.controller_services = ControllerServices(
            run_trusted_agent=service_impl.run_trusted_agent,
            materialize_variant=service_impl.materialize_variant,
            evaluate_public_variant=service_impl.evaluate_public_variant,
            select_by_validation=service_impl.select_by_validation,
            register_finalist=service_impl.register_finalist,
        )

    def run(self) -> EvolveResult:
        self._initialize_run_directory()
        self.events = EventWriter(self.run_dir / "events.jsonl")
        provenance = build_run_provenance(
            self.config,
            worker_adapter=self.worker_adapter,
            worker_provenance=self.worker_adapter.provenance(self.config.workers.pool),
            evaluator_factory=self.evaluator_factory,
        )
        _write_json(self.run_dir / "resolved_config.json", self.config.redacted_dict())
        _write_json(self.run_dir / "provenance.json", provenance)
        self._emit("run_started", "running", data=provenance)
        try:
            self._run_phases()
            if self.result is None:
                raise RuntimeError("Workflow completed without producing EvolveResult.")
            self._emit(
                "run_finished",
                "succeeded",
                data={"best_candidate_id": self.result.best_candidate_id},
            )
            self._refresh_status()
            render_final_report(
                self.run_dir / "public_events.jsonl", self.run_dir / "report.html"
            )
            return self.result
        except Exception as exc:
            self._emit(
                "run_finished",
                "failed",
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            self._refresh_status()
            raise
        finally:
            if self.events is not None:
                self.events.close()
                self.events = None

    def _run_phases(self) -> None:
        for spec, phase in zip(self.config.workflow.phases, self.phases, strict=True):
            missing = phase.requires.difference(self.artifacts)
            if missing:
                raise RuntimeError(
                    f"Phase {phase.name!r} requires missing artifacts {sorted(missing)!r}."
                )
            scope = {"phase": phase.name}
            self._emit("phase_started", "running", scope=scope)
            try:
                context = PhaseContext(
                    run_dir=self.run_dir,
                    options=spec.options,
                    artifacts=MappingProxyType(dict(self.artifacts)),
                    invoke=self._invoke_core_phase,
                    emit=self._scoped_emitter(scope),
                    services=self.controller_services,
                )
                result = phase.run(context)
                undeclared = set(result.artifacts).difference(phase.provides)
                if undeclared:
                    raise RuntimeError(
                        f"Phase {phase.name!r} produced undeclared artifacts "
                        f"{sorted(undeclared)!r}."
                    )
                self.artifacts.update(result.artifacts)
                self._emit(
                    "phase_finished",
                    result.status,
                    scope=scope,
                    data={"artifacts": sorted(result.artifacts)},
                )
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

    def _invoke_core_phase(self, action: str) -> Mapping[str, object]:
        actions: Mapping[str, Callable[[], Mapping[str, object]]] = {
            "prepare": self._phase_prepare,
            "explore": self._phase_explore,
            "validate": self._phase_validate,
            "hidden": self._phase_hidden,
            "report": self._phase_report,
        }
        try:
            return actions[action]()
        except KeyError as exc:
            raise ValueError(f"Unknown core phase action: {action!r}") from exc

    def _phase_prepare(self) -> Mapping[str, object]:
        self.reference = materialize_reference(
            initial=self.initial,
            destination=self.run_dir / "controller" / "reference",
            interface=self.interface,
            config=self.config.evaluation,
        )
        seed_path = self.run_dir / "candidates" / "seed"
        copy_initial_source(self.initial, seed_path)
        self._validate_candidate(seed_path)
        self.data = resolve_data_plan(self.config.data)
        write_data_manifests(self.data, self.run_dir)
        solver_contract = self.run_dir / "solver_contract.json"
        _write_json(
            solver_contract,
            {
                "source": str(self.initial),
                "interface": f"{self.interface.file}:{self.interface.function}",
                "runtime": self.runtime,
                "editable": list(self.editable),
            },
        )
        self.evaluator = self.evaluator_factory(
            runtime=self.runtime,
            reference=self.reference,
            interface=self.interface,
            data=self.data,
            config=self.config.evaluation,
        )
        seed_public_mode = "public" if self.config.evaluation.feedback_mode == "agent" else "public_score"
        seed_public = self.evaluator.evaluate(
            seed_path,
            seed_public_mode,
            self.run_dir / "controller" / "evaluations" / "seed" / "public",
        )
        if not seed_public.success:
            raise RuntimeError(f"Initial solver evaluation failed: {seed_public.error}")
        seed_validation = self.evaluator.evaluate(
            seed_path,
            "validation",
            self.run_dir / "controller" / "evaluations" / "seed" / "validation",
        )
        if not seed_validation.success:
            raise RuntimeError(f"Initial solver validation failed: {seed_validation.error}")
        seed = CandidateRecord(
            candidate_id="seed",
            island=-1,
            iteration=0,
            attempt_index=0,
            parent_id=None,
            path=seed_path,
            tree_hash=tree_hash(seed_path),
            public_score=seed_public.score,
            validation_score=seed_validation.score,
        )
        self.candidates[seed.candidate_id] = seed
        self.populations = [[seed] for _ in range(self.config.evolution.islands)]
        self._write_checkpoint(0)
        return {
            "prepared": {
                "seed": str(seed_path),
                "seed_public_evidence": str(seed_public.output_dir),
                "solver_contract": str(solver_contract),
                "islands": self.config.evolution.islands,
                "interface": f"{self.interface.file}:{self.interface.function}",
                "runtime": self.runtime,
                "editable": list(self.editable),
            }
        }

    def _phase_explore(self) -> Mapping[str, object]:
        if self.evaluator is None or self.data is None:
            raise RuntimeError("Explore phase requires prepared evaluator and data.")
        self.direction_assignments = _direction_assignments(self.artifacts.get("directions"))
        should_stop = False
        for iteration in range(1, self.config.evolution.iterations + 1):
            iteration_scope = {"phase": "explore", "iteration": iteration}
            self._emit("iteration_started", "running", scope=iteration_scope)
            jobs: list[tuple[int, int, CandidateRecord, WorkerConfig]] = []
            for island, population in enumerate(self.populations):
                for attempt_index in range(self.config.evolution.attempts_per_island):
                    parent_rng = random.Random(
                        coordinate_seed(
                            self.config.evolution.random_seed,
                            "explore",
                            iteration,
                            island,
                            attempt_index,
                            "parent",
                        )
                    )
                    worker_rng = random.Random(
                        coordinate_seed(
                            self.config.evolution.random_seed,
                            "explore",
                            iteration,
                            island,
                            attempt_index,
                            "worker",
                        )
                    )
                    jobs.append(
                        (
                            island,
                            attempt_index,
                            self._select_parent(population, parent_rng),
                            self._select_worker(worker_rng),
                        )
                    )

            attempts: list[_Attempt] = []
            max_workers = min(self.config.workers.max_parallel, len(jobs))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        self._run_attempt,
                        iteration=iteration,
                        island=island,
                        attempt_index=attempt_index,
                        parent=parent,
                        worker=worker,
                    ): (island, attempt_index, parent, worker)
                    for island, attempt_index, parent, worker in jobs
                }
                for future in concurrent.futures.as_completed(futures):
                    island, attempt_index, parent, worker = futures[future]
                    try:
                        attempts.append(future.result())
                    except Exception as exc:
                        attempts.append(
                            self._failed_attempt(
                                iteration,
                                island,
                                attempt_index,
                                parent,
                                worker,
                                exc,
                            )
                        )

            # Explicit iteration barrier: only the engine accepts and records.
            for attempt in sorted(
                attempts,
                key=lambda item: (item.record.island, item.record.attempt_index),
            ):
                self._accept_and_record(attempt)

            view = IterationView(
                iteration=iteration,
                populations=tuple(tuple(self._trim_population(pop)) for pop in self.populations),
                attempt_ids=tuple(item.record.attempt_id for item in attempts),
                rng_seed=coordinate_seed(
                    self.config.evolution.random_seed, "explore", iteration, "after_iteration"
                ),
            )
            for index, policy in enumerate(self.after_iteration):
                policy_scope = {
                    "phase": "explore",
                    "iteration": iteration,
                    "step": policy.name,
                    "step_idx": index,
                }
                self._emit("policy_started", "running", scope=policy_scope)
                try:
                    edit = policy.propose(view)
                    self._apply_population_edit(edit)
                except Exception as exc:
                    self._emit(
                        "policy_finished",
                        "failed",
                        scope=policy_scope,
                        data={"error": f"{type(exc).__name__}: {exc}"},
                    )
                    raise
                else:
                    self._emit(
                        "policy_finished",
                        "succeeded",
                        scope=policy_scope,
                        data={
                            "kill": list(edit.kill),
                            "migrate": [list(item) for item in edit.migrate],
                            "budget": dict(edit.budget),
                            "stop": edit.stop,
                            "reason": edit.reason,
                        },
                    )
                should_stop = should_stop or edit.stop
            self._write_checkpoint(iteration)
            self._emit(
                "iteration_finished",
                "succeeded",
                scope=iteration_scope,
                data={"stop": should_stop, "attempt_count": len(attempts)},
            )
            self._refresh_status()
            if should_stop:
                break
        return {"populations": tuple(tuple(pop) for pop in self.populations)}

    def _phase_validate(self) -> Mapping[str, object]:
        finalists = self._fixed_finalists(self.populations)
        finalists.extend(
            (record.island, record)
            for record in self.research_finalists.values()
            if record.valid
        )
        finalists = list(
            {
                record.candidate_id: (island, record)
                for island, record in finalists
            }.values()
        )
        if not finalists:
            raise RuntimeError("No valid finalists were produced.")
        self.champion = max(
            finalists,
            key=lambda item: (
                item[1].validation_score,
                item[1].public_score,
                item[1].iteration,
                item[1].candidate_id,
            ),
        )
        _write_json(
            self.run_dir / "validation_selection.json",
            {
                "champion": self.champion[1].candidate_id,
                "finalists": [record.candidate_id for _, record in finalists],
            },
        )
        return {"champion": self.champion}

    def _phase_hidden(self) -> Mapping[str, object]:
        if self.champion is None or self.evaluator is None or self.data is None:
            raise RuntimeError("Hidden phase requires a validation-selected champion.")
        island, champion = self.champion
        self.final_result = self._evaluate_hidden_champion(
            island, champion, self.evaluator, self.data
        )
        if not self.final_result.success:
            raise RuntimeError(
                f"Controller-only hidden evaluation failed: {self.final_result.error}"
            )
        self.final_solver = self.run_dir / "final_solver"
        copy_solver_tree(champion.path, self.final_solver)
        return {"final": self.final_result}

    def _phase_report(self) -> Mapping[str, object]:
        if self.final_result is None or self.final_solver is None:
            raise RuntimeError("Report phase requires a hidden evaluation result.")
        _write_json(self.run_dir / "final_ranking.json", [self.final_result.as_dict()])
        self._write_final_report(self.final_result, [self.final_result], self.final_solver)
        self.result = EvolveResult(
            run_dir=self.run_dir,
            best_solver=self.final_solver,
            best_candidate_id=self.final_result.candidate_id,
            public_score=self.final_result.public_score,
            validation_score=self.final_result.validation_score,
            final_score=self.final_result.final_score,
            finalists=(self.final_result,),
        )
        return {"result": self.result, "report": self.run_dir / "FINAL_REPORT.md"}

    def _run_attempt(
        self,
        *,
        iteration: int,
        island: int,
        attempt_index: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
    ) -> _Attempt:
        attempt_id = f"it{iteration:03d}-i{island:02d}-a{attempt_index:02d}"
        scope = {
            "phase": "explore",
            "iteration": iteration,
            "island": island,
            "attempt_id": attempt_id,
        }
        self._emit(
            "attempt_started",
            "running",
            scope=scope,
            data={
                "parent_id": parent.candidate_id,
                "worker": f"{worker.harness}:{worker.model}",
                "guidance": _guidance_id(self.direction_assignments.get(island)),
            },
        )
        workspace = self.run_dir / "workspaces" / attempt_id
        copy_solver_tree(parent.path, workspace)
        capabilities = _EngineAttemptCapabilities(
            engine=self,
            attempt_id=attempt_id,
            iteration=iteration,
            island=island,
            attempt_index=attempt_index,
            parent=parent,
            worker=worker,
            workspace=workspace,
        )
        results: list[StepResult] = []
        rejected = False
        for step_index, step in enumerate(self.attempt_steps):
            step_scope = {**scope, "step": step.name, "step_idx": step_index}
            self._emit("step_started", "running", scope=step_scope)
            context = AttemptContext(
                attempt_id=attempt_id,
                iteration=iteration,
                island=island,
                attempt_index=attempt_index,
                parent_id=parent.candidate_id,
                parent_score=parent.public_score,
                workspace=workspace,
                worker_name=f"{worker.harness}:{worker.model}",
                rng_seed=coordinate_seed(
                    self.config.evolution.random_seed,
                    "explore",
                    iteration,
                    island,
                    attempt_index,
                    step.name,
                ),
                prior_results=tuple(results),
                capabilities=capabilities,
                emit=self._scoped_emitter(step_scope),
            )
            try:
                result = step.run(context)
            except Exception as exc:
                result = StepResult(
                    verdict="reject",
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            status = "failed" if result.verdict == "reject" else "succeeded"
            self._emit(
                "step_finished",
                status,
                scope=step_scope,
                data={
                    "verdict": result.verdict,
                    "metrics": dict(result.metrics),
                    "artifacts": list(result.artifacts),
                    "error": result.error,
                },
            )
            if result.verdict == "reject":
                rejected = True
                break

        changed = capabilities.changed
        outcome = capabilities.worker_outcome or WorkerOutcome(
            1,
            self.run_dir / "transcripts" / f"{attempt_id}.jsonl",
        )
        metrics = {key: value for result in results for key, value in result.metrics.items()}
        try:
            if rejected:
                error = next((result.error for result in reversed(results) if result.error), None)
                raise RuntimeError(error or "attempt pipeline rejected candidate")
            # Non-removable safety and controller-only validation gate.
            changed = capabilities.audit_candidate()
            public_score = float(metrics["public_score"])
            if self.evaluator is None:
                raise RuntimeError("Evaluator is unavailable.")
            validation = self.evaluator.evaluate(
                workspace,
                "validation",
                self.run_dir / "controller" / "evaluations" / attempt_id / "validation",
            )
            if not validation.success:
                raise RuntimeError(validation.error or "controller validation evaluation failed")
            snapshot = self.run_dir / "candidates" / attempt_id
            copy_solver_tree(workspace, snapshot)
            record = CandidateRecord(
                candidate_id=attempt_id,
                island=island,
                iteration=iteration,
                attempt_index=attempt_index,
                parent_id=parent.candidate_id,
                path=snapshot,
                tree_hash=tree_hash(snapshot),
                public_score=public_score,
                validation_score=validation.score,
                worker=f"{worker.harness}:{worker.model}",
                guidance=_guidance_id(self.direction_assignments.get(island)),
            )
        except Exception as exc:
            record = CandidateRecord(
                candidate_id=attempt_id,
                island=island,
                iteration=iteration,
                attempt_index=attempt_index,
                parent_id=parent.candidate_id,
                path=workspace,
                tree_hash="",
                public_score=float(metrics.get("public_score", 0.0)),
                validation_score=0.0,
                worker=f"{worker.harness}:{worker.model}",
                guidance=_guidance_id(self.direction_assignments.get(island)),
                valid=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return _Attempt(record, changed, outcome, tuple(results))

    def _failed_attempt(
        self,
        iteration: int,
        island: int,
        attempt_index: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
        exc: Exception,
    ) -> _Attempt:
        attempt_id = f"it{iteration:03d}-i{island:02d}-a{attempt_index:02d}"
        transcript = self.run_dir / "transcripts" / f"{attempt_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            f"[controller] attempt setup failed: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        record = CandidateRecord(
            candidate_id=attempt_id,
            island=island,
            iteration=iteration,
            attempt_index=attempt_index,
            parent_id=parent.candidate_id,
            path=self.run_dir / "workspaces" / attempt_id,
            tree_hash="",
            public_score=0.0,
            validation_score=0.0,
            worker=f"{worker.harness}:{worker.model}",
            guidance=_guidance_id(self.direction_assignments.get(island)),
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _Attempt(record, (), WorkerOutcome(1, transcript), ())

    def _accept_and_record(self, attempt: _Attempt) -> None:
        record = attempt.record
        self.candidates[record.candidate_id] = record
        summary = {
            "attempt_id": record.attempt_id,
            "candidate_id": record.candidate_id,
            "island": record.island,
            "iteration": record.iteration,
            "attempt_index": record.attempt_index,
            "parent_id": record.parent_id,
            "guidance": record.guidance,
            "public_score": record.public_score,
            "validation_score": record.validation_score,
            "valid": record.valid,
            "error": record.error,
            "changed_files": list(attempt.changed),
            "worker_returncode": attempt.worker_outcome.returncode,
            "worker_timed_out": attempt.worker_outcome.timed_out,
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
            "trace_capture_error": attempt.worker_outcome.capture_error,
        }
        self.attempt_history.append(summary)
        _append_json(self.run_dir / "attempts.jsonl", summary)
        accepted = False
        if record.valid:
            population = self.populations[record.island]
            population.append(record)
            self.populations[record.island] = self._trim_population(population)
            accepted = record.candidate_id in {
                item.candidate_id for item in self.populations[record.island]
            }
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
        if killed:
            self.populations = [
                [record for record in population if record.candidate_id not in killed]
                for population in self.populations
            ]
        for candidate_id, target in edit.migrate:
            if not 0 <= target < len(self.populations):
                raise ValueError(f"Migration target island is out of range: {target}")
            try:
                record = self.candidates[candidate_id]
            except KeyError as exc:
                raise ValueError(
                    f"Migration references unknown candidate {candidate_id!r}."
                ) from exc
            self.populations[target] = self._trim_population([*self.populations[target], record])

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

    def _select_parent(
        self, population: list[CandidateRecord], rng: random.Random
    ) -> CandidateRecord:
        ordered = self._trim_population(population)
        if len(ordered) == 1 or rng.random() < 0.7:
            return ordered[0]
        weights = [max(record.validation_score, 1e-6) for record in ordered]
        return rng.choices(ordered, weights=weights, k=1)[0]

    def _select_worker(self, rng: random.Random) -> WorkerConfig:
        weighted = [worker for worker in self.config.workers.pool for _ in range(worker.weight)]
        return rng.choice(weighted)

    def _trim_population(self, population: list[CandidateRecord]) -> list[CandidateRecord]:
        unique = {record.candidate_id: record for record in population}
        ordered = sorted(
            unique.values(),
            key=lambda record: (
                -record.validation_score,
                -record.public_score,
                -record.iteration,
                record.candidate_id,
            ),
        )
        return ordered[: self.config.evolution.population_per_island]

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

    def _evaluate_hidden_champion(
        self,
        island: int,
        record: CandidateRecord,
        evaluator: Evaluator,
        data: DataPlan,
    ) -> FinalistResult:
        output_dir = self.run_dir / "controller" / "final_evaluations" / record.candidate_id
        if data.hidden:
            result = evaluator.evaluate(record.path, "hidden", output_dir)
            final_score = result.score if result.success else 0.0
            success = result.success
            error = result.error
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            final_score = record.validation_score
            success = True
            error = None
        return FinalistResult(
            candidate_id=record.candidate_id,
            island=island,
            public_score=record.public_score,
            validation_score=record.validation_score,
            final_score=final_score,
            output_dir=output_dir,
            success=success,
            error=error,
        )

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
                key=lambda item: item["public_score"],
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

    def _write_final_report(
        self,
        best: FinalistResult,
        finalists: list[FinalistResult],
        final_solver: Path,
    ) -> None:
        lines = [
            "# Evolution Result",
            "",
            f"- Best candidate: `{best.candidate_id}`",
            f"- Public fitness: `{best.public_score:.6f}`",
            f"- Validation fitness: `{best.validation_score:.6f}`",
            f"- Hidden fitness: `{best.final_score:.6f}`",
            f"- Materialized solver: `{final_solver}`",
            "",
            "## Validation-selected champion",
            "",
            "| Candidate | Island | Public | Validation | Hidden | Status |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for item in sorted(finalists, key=lambda value: -value.final_score):
            lines.append(
                f"| `{item.candidate_id}` | {item.island} | "
                f"{item.public_score:.6f} | {item.validation_score:.6f} | "
                f"{item.final_score:.6f} | "
                f"{'ok' if item.success else item.error or 'failed'} |"
            )
        research_artifacts = [
            path
            for path in (
                self.run_dir / "research" / "directions.json",
                self.run_dir / "research" / "analysis" / "island_bundles.json",
                self.run_dir / "research" / "recombination.json",
                self.run_dir / "research" / "challenger_report.json",
            )
            if path.is_file()
        ]
        if research_artifacts:
            lines.extend(["", "## Research workflow artifacts", ""])
            lines.extend(
                f"- [`{path.relative_to(self.run_dir).as_posix()}`]({path.relative_to(self.run_dir).as_posix()})"
                for path in research_artifacts
            )
        (self.run_dir / "FINAL_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

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

    def _refresh_status(self) -> None:
        if self.events is None:
            return
        self.events.flush()
        public_events = self.run_dir / "public_events.jsonl"
        project_public_events(self.run_dir / "events.jsonl", public_events)
        render_status(public_events, self.run_dir / "status.html")


class _EngineControllerServices:
    """Engine-owned implementation of the five trusted phase capabilities."""

    def __init__(self, engine: EvolutionEngine) -> None:
        self.engine = engine

    def run_trusted_agent(self, job: AgentJob) -> AgentJobResult:
        engine = self.engine
        role = _safe_identifier(job.role, "role")
        job_id = _safe_identifier(job.job_id, "job_id")
        if not 0 <= job.worker_index < len(engine.config.workers.pool):
            raise ValueError(f"Agent job worker_index is out of range: {job.worker_index}")
        workspace = engine.run_dir / "research" / "roles" / role / job_id
        if workspace.exists():
            raise FileExistsError(f"Role workspace already exists: {workspace}")
        workspace.mkdir(parents=True)
        replacements = {
            str(engine.run_dir): "<run_dir>",
            str(engine.initial): "<initial_solver>",
        }
        for relative, source in job.inputs.items():
            destination = workspace / safe_relative_path(relative)
            _copy_role_input(Path(source), destination)
            if relative not in {"seed", "finalist", "parent"}:
                _redact_role_input(destination, replacements)
        validate_tree_safety(
            workspace,
            max_files=engine.config.sandbox.max_candidate_files * 8,
            max_bytes=engine.config.sandbox.max_candidate_bytes * 8,
        )

        tools = job.tools or ToolConfig(
            preset="minimal",
            web_search=False,
            network=False,
            compilers=False,
        )
        tools.validate()
        worker = engine.config.workers.pool[job.worker_index]
        workers = replace(
            engine.config.workers,
            pool=(worker,),
            tools=tools,
            timeout_seconds=job.timeout_seconds or engine.config.workers.timeout_seconds,
            token_budget=(
                job.token_budget if job.token_budget is not None else engine.config.workers.token_budget
            ),
            max_budget_usd=(
                job.max_budget_usd
                if job.max_budget_usd is not None
                else engine.config.workers.max_budget_usd
            ),
        )
        tools_dir = engine.run_dir / "controller" / "role_tools" / role / job_id
        tools_dir.mkdir(parents=True, exist_ok=True)
        broker_root = engine.run_dir / "controller" / "role_brokers" / role / job_id
        exchange = broker_root / "exchange"
        artifacts = broker_root / "artifacts"
        (exchange / "requests").mkdir(parents=True, exist_ok=True)
        (exchange / "responses").mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        if engine.config.sandbox.backend == "docker":
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
        transcript = engine.run_dir / "research" / "transcripts" / role / f"{job_id}.jsonl"
        role_phase = {
            "direction-scout": "direction_scout",
            "strategy-analyst": "strategy_analysis",
        }.get(role, role)
        scope: dict[str, object] = {"phase": role_phase, "role": role, "job_id": job_id}
        island_match = re.search(r"island-(\d+)", job_id)
        if island_match:
            scope["island"] = int(island_match.group(1))
        engine._emit(
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
            outcome = engine.worker_adapter.run(
                WorkerRequest(
                    worker=worker,
                    workers=workers,
                    sandbox=engine.config.sandbox,
                    workspace=workspace,
                    tools_dir=tools_dir,
                    broker=connection,
                    prompt=job.prompt,
                    transcript=transcript,
                    trace_dir=engine.run_dir / "research" / "traces" / role / job_id,
                    trace_context={
                        "schema": "trace_input/1",
                        "join": {
                            "job_id": job_id,
                            "role": role,
                            "phase": role_phase,
                            "island": scope.get("island"),
                        },
                        "inputs": sorted(job.inputs),
                    },
                )
            )
        except Exception as exc:
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                f"[controller] role launch failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            outcome = WorkerOutcome(1, transcript)
        validate_tree_safety(
            workspace,
            max_files=engine.config.sandbox.max_candidate_files * 8,
            max_bytes=engine.config.sandbox.max_candidate_bytes * 8,
        )
        outputs: dict[str, Path] = {}
        for relative in job.expected_outputs:
            path = (workspace / safe_relative_path(relative)).resolve()
            if workspace.resolve() not in path.parents or not path.exists():
                continue
            outputs[relative] = path
        missing_outputs = sorted(set(job.expected_outputs).difference(outputs))
        succeeded = outcome.returncode == 0 and not outcome.timed_out and not missing_outputs
        engine._emit(
            "role_agent_finished",
            "succeeded" if succeeded else "failed",
            scope=scope,
            data={
                "returncode": outcome.returncode,
                "timed_out": outcome.timed_out,
                "transcript": str(outcome.transcript),
                "native_trace": str(outcome.native_trace) if outcome.native_trace else None,
                "stderr_trace": str(outcome.stderr_trace) if outcome.stderr_trace else None,
                "trace_chunks": str(outcome.trace_chunks) if outcome.trace_chunks else None,
                "trace_capture_error": outcome.capture_error,
                "outputs": sorted(outputs),
                "missing_outputs": missing_outputs,
            },
        )
        if not succeeded:
            details = []
            if outcome.returncode != 0:
                details.append(f"returncode={outcome.returncode}")
            if outcome.timed_out:
                details.append("timed_out=true")
            if missing_outputs:
                details.append(f"missing_outputs={missing_outputs!r}")
            raise RuntimeError(
                f"Trusted role {role!r} did not complete successfully ({', '.join(details)})."
            )
        return AgentJobResult(workspace, outcome, MappingProxyType(outputs))

    def materialize_variant(self, request: VariantRequest) -> VariantHandle:
        engine = self.engine
        variant_id = _safe_identifier(request.variant_id, "variant_id")
        if request.tree is not None and request.patches:
            raise ValueError("A variant request may provide a tree or patches, not both.")
        base = request.base.resolve()
        if not base.is_dir():
            raise FileNotFoundError(f"Variant base does not exist: {base}")
        base_hash = tree_hash(base)
        if request.expected_base_hash and request.expected_base_hash != base_hash:
            raise ValueError("Variant base tree hash does not match the declared base.")
        destination = engine.run_dir / "research" / "variants" / variant_id
        staging = engine.run_dir / "research" / "variant_staging" / variant_id
        if destination.exists() or staging.exists() or variant_id in engine.variant_handles:
            raise FileExistsError(f"Variant id already exists: {variant_id}")
        change_hashes: list[str] = []
        try:
            if request.tree is not None:
                source_tree = request.tree.resolve()
                _require_inside(source_tree, engine.run_dir / "research")
                copy_solver_tree(source_tree, staging)
                change_hashes.append(tree_hash(source_tree))
            else:
                copy_solver_tree(base, staging)
                for patch in request.patches:
                    patch = patch.resolve()
                    _require_inside(patch, engine.run_dir / "research")
                    if not patch.is_file():
                        raise FileNotFoundError(f"Variant patch does not exist: {patch}")
                    _validate_patch_paths(patch)
                    _apply_patch(staging, patch)
                    change_hashes.append(file_hash(patch))
            engine._validate_candidate(staging)
            changed = changed_files(base, staging)
            if not changed:
                raise ValueError("Executable variant produced no solver-tree change.")
            validate_edit_scope(changed, engine.editable)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(destination)
            handle = VariantHandle(
                variant_id=variant_id,
                path=destination,
                tree_hash=tree_hash(destination),
                base_tree_hash=base_hash,
                change_hashes=tuple(change_hashes),
            )
            engine.variant_handles[variant_id] = handle
            _write_json(
                engine.run_dir / "research" / "variant_manifests" / f"{variant_id}.json",
                {
                    "schema": "variant_manifest/1",
                    "variant_id": variant_id,
                    "base_tree_hash": base_hash,
                    "change_hashes": list(handle.change_hashes),
                    "tree_hash": handle.tree_hash,
                    "changed_files": list(changed),
                },
            )
            engine._emit(
                "variant_materialized",
                "succeeded",
                scope={"variant_id": variant_id},
                data={
                    "base_tree_hash": base_hash,
                    "tree_hash": handle.tree_hash,
                    "change_hashes": list(handle.change_hashes),
                    "changed_files": list(changed),
                },
            )
            return handle
        except Exception as exc:
            if staging.exists():
                shutil.rmtree(staging)
            engine._emit(
                "variant_materialized",
                "failed",
                scope={"variant_id": variant_id},
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise

    def evaluate_public_variant(
        self,
        handle: VariantHandle,
        label: str,
        *,
        reference: str | None = None,
    ) -> EvaluationResult:
        engine = self.engine
        label = _safe_identifier(label, "evaluation label")
        if tree_hash(handle.path) != handle.tree_hash:
            raise ValueError(f"Variant tree changed after materialization: {handle.variant_id}")
        if engine.evaluator is None or engine.data is None:
            raise RuntimeError("Public variant evaluation requires a prepared evaluator.")
        evaluator = engine.evaluator
        mode = "public_score"
        if reference is not None:
            if reference not in {"initial", "scipy_powell", "prima_newuoa"}:
                raise ValueError(f"Unsupported challenger reference: {reference!r}")
            challenger_root = engine.run_dir / "controller" / "challengers" / reference
            if not challenger_root.exists():
                materialize_reference(
                    initial=engine.initial,
                    destination=challenger_root,
                    interface=engine.interface,
                    config=replace(engine.config.evaluation, reference=reference),
                )
            evaluator = engine.evaluator_factory(
                runtime=engine.runtime,
                reference=challenger_root,
                interface=engine.interface,
                data=engine.data,
                config=replace(engine.config.evaluation, reference=reference),
            )
            mode = "public"
        output_dir = (
            engine.run_dir
            / "controller"
            / "research_evaluations"
            / handle.variant_id
            / label
        )
        result = evaluator.evaluate(handle.path, mode, output_dir)
        if reference is None:
            engine.variant_public[handle.variant_id] = result
        engine._emit(
            "research_evaluation_finished",
            "succeeded" if result.success else "failed",
            scope={"variant_id": handle.variant_id, "evaluation": label},
            data={
                "mode": "challenger_public" if reference else "public",
                "reference": reference or engine.config.evaluation.reference,
                "output_dir": str(result.output_dir),
                "success": result.success,
            },
        )
        return result

    def select_by_validation(self, candidate_ids: Sequence[str], limit: int) -> tuple[str, ...]:
        engine = self.engine
        if limit < 1:
            raise ValueError("Validation selection limit must be positive.")
        engine.validation_selection_count += 1
        unique = tuple(dict.fromkeys(str(value) for value in candidate_ids))
        ranked: list[tuple[float, float, str]] = []
        new_queries = 0
        for candidate_id in unique:
            record = engine.candidates.get(candidate_id)
            if record is not None:
                ranked.append((record.validation_score, record.public_score, candidate_id))
                continue
            handle = engine.variant_handles.get(candidate_id)
            if handle is None:
                raise KeyError(f"Validation selection references unknown candidate {candidate_id!r}.")
            result = engine.variant_validation.get(candidate_id)
            if result is None:
                if engine.evaluator is None:
                    raise RuntimeError("Validation selection requires a prepared evaluator.")
                result = engine.evaluator.evaluate(
                    handle.path,
                    "validation",
                    engine.run_dir
                    / "controller"
                    / "research_evaluations"
                    / candidate_id
                    / "validation",
                )
                engine.variant_validation[candidate_id] = result
                engine.validation_query_count += 1
                new_queries += 1
            public = engine.variant_public.get(candidate_id)
            ranked.append(
                (
                    result.score if result.success else 0.0,
                    public.score if public is not None and public.success else 0.0,
                    candidate_id,
                )
            )
        selected = tuple(
            candidate_id
            for _validation, _public, candidate_id in sorted(
                ranked,
                key=lambda item: (-item[0], -item[1], item[2]),
            )[:limit]
        )
        query = {
            "candidate_ids": list(unique),
            "selected_ids": list(selected),
            "new_evaluations": new_queries,
            "cumulative_evaluations": engine.validation_query_count,
        }
        _append_json(engine.run_dir / "controller" / "validation_queries.jsonl", query)
        _write_json(
            engine.run_dir / "research" / "validation_usage.json",
            {
                "schema": "validation_usage/1",
                "selection_calls": engine.validation_selection_count,
                "new_evaluations": engine.validation_query_count,
                "last_selection_candidate_count": len(unique),
                "last_selection_new_evaluations": new_queries,
            },
        )
        engine._emit(
            "validation_selection_finished",
            "succeeded",
            data=query,
        )
        return selected

    def register_finalist(
        self,
        handle: VariantHandle,
        metadata: Mapping[str, object],
    ) -> str:
        engine = self.engine
        if handle.variant_id not in engine.variant_handles:
            raise ValueError("Only a gated controller variant can be registered as a finalist.")
        validation = engine.variant_validation.get(handle.variant_id)
        if validation is None or not validation.success:
            raise ValueError("A research finalist must pass controller validation first.")
        public = engine.variant_public.get(handle.variant_id)
        public_score = (
            public.score
            if public is not None and public.success
            else float(metadata.get("public_score", 0.0))
        )
        existing = engine.candidates.get(handle.variant_id)
        if existing is not None:
            if existing.tree_hash != handle.tree_hash:
                raise ValueError("Candidate id is already registered with another tree.")
            return existing.candidate_id
        record = CandidateRecord(
            candidate_id=handle.variant_id,
            island=int(metadata.get("island", -2)),
            iteration=engine.config.evolution.iterations + 1,
            attempt_index=len(engine.research_finalists),
            parent_id=str(metadata["parent_id"]) if metadata.get("parent_id") else None,
            path=handle.path,
            tree_hash=handle.tree_hash,
            public_score=public_score,
            validation_score=validation.score,
            worker="controller:research",
        )
        engine.candidates[record.candidate_id] = record
        engine.research_finalists[record.candidate_id] = record
        _append_json(
            engine.run_dir / "research" / "registered_finalists.jsonl",
            {
                "candidate_id": record.candidate_id,
                "island": record.island,
                "parent_id": record.parent_id,
                "tree_hash": record.tree_hash,
                "public_score": record.public_score,
                "source": metadata.get("source"),
            },
        )
        engine._emit(
            "research_finalist_registered",
            "succeeded",
            scope={"variant_id": record.candidate_id, "island": record.island},
            data={"tree_hash": record.tree_hash, "source": metadata.get("source")},
        )
        return record.candidate_id


class _EngineAttemptCapabilities(AttemptCapabilities):
    """Public-only attempt services; validation and hidden are intentionally absent."""

    def __init__(
        self,
        *,
        engine: EvolutionEngine,
        attempt_id: str,
        iteration: int,
        island: int,
        attempt_index: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
        workspace: Path,
    ) -> None:
        self._engine = engine
        self._attempt_id = attempt_id
        self._iteration = iteration
        self._island = island
        self._attempt_index = attempt_index
        self._parent = parent
        self._worker = worker
        self._workspace = workspace
        self.changed: tuple[str, ...] = ()
        self.worker_outcome: WorkerOutcome | None = None

    def run_worker(self) -> WorkerOutcome:
        engine = self._engine
        if engine.evaluator is None or engine.data is None:
            raise RuntimeError("Worker capability requires prepared evaluator and data.")
        tools_dir = engine.run_dir / "controller" / "worker_tools" / self._attempt_id
        transcript = engine.run_dir / "transcripts" / f"{self._attempt_id}.jsonl"
        broker = EvaluationBroker(
            workspace=self._workspace,
            control_dir=engine.run_dir / "controller" / "brokers" / self._attempt_id,
            evaluator=engine.evaluator,
            max_smoke_calls=engine.config.evaluation.max_smoke_calls_per_worker,
            max_public_calls=engine.config.evaluation.max_public_calls_per_worker,
            candidate_validator=self._ensure_candidate_safe,
        )
        broker.install_tools(tools_dir)
        connection = broker.start(docker=engine.config.sandbox.backend == "docker")
        prompt = build_worker_prompt(
            interface=engine.interface,
            runtime=engine.runtime,
            editable=engine.editable,
            data=engine.data,
            iteration=self._iteration,
            island=self._island,
            parent_score=self._parent.public_score,
            controller_memory=engine._memory_for(self._island),
            token_budget=engine.config.workers.token_budget,
            max_smoke_calls=engine.config.evaluation.max_smoke_calls_per_worker,
            max_public_calls=engine.config.evaluation.max_public_calls_per_worker,
            forbidden_candidate_imports=engine.config.evaluation.forbidden_candidate_imports,
            direction=engine.direction_assignments.get(self._island),
        )
        try:
            engine._emit(
                "worker_started",
                "running",
                scope=self._scope(),
                data={"worker": f"{self._worker.harness}:{self._worker.model}"},
            )
            outcome = engine.worker_adapter.run(
                WorkerRequest(
                    worker=self._worker,
                    workers=engine.config.workers,
                    sandbox=engine.config.sandbox,
                    workspace=self._workspace,
                    tools_dir=tools_dir,
                    broker=connection,
                    prompt=prompt,
                    transcript=transcript,
                    trace_dir=engine.run_dir / "traces" / self._attempt_id,
                    trace_context={
                        "schema": "trace_input/1",
                        "join": {
                            "attempt_id": self._attempt_id,
                            "iteration": self._iteration,
                            "island": self._island,
                            "attempt_index": self._attempt_index,
                        },
                        "parent_id": self._parent.candidate_id,
                        "parent_tree_hash": self._parent.tree_hash,
                    },
                )
            )
        except Exception as exc:
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                f"[controller] worker launch failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            outcome = WorkerOutcome(1, transcript)
        finally:
            broker.stop()
        self.worker_outcome = outcome
        engine._emit(
            "worker_finished",
            "failed" if outcome.returncode else "succeeded",
            scope=self._scope(),
            data={
                "returncode": outcome.returncode,
                "timed_out": outcome.timed_out,
                "transcript": str(outcome.transcript),
                "native_trace": str(outcome.native_trace) if outcome.native_trace else None,
                "stderr_trace": str(outcome.stderr_trace) if outcome.stderr_trace else None,
                "trace_chunks": str(outcome.trace_chunks) if outcome.trace_chunks else None,
                "trace_capture_error": outcome.capture_error,
            },
        )
        return outcome

    def audit_candidate(self) -> tuple[str, ...]:
        self._ensure_candidate_safe(self._workspace)
        changed = changed_files(self._parent.path, self._workspace)
        if not changed:
            raise ValueError("Worker produced no solver changes.")
        validate_edit_scope(changed, self._engine.editable)
        self.changed = changed
        return changed

    def evaluate_public(self, mode: str) -> EvaluationResult:
        if mode not in {"smoke", "public", "public_score"}:
            raise ValueError(f"Attempt steps cannot evaluate controller-only mode {mode!r}.")
        self.audit_candidate()
        if self._engine.evaluator is None:
            raise RuntimeError("Evaluator is unavailable.")
        directory_name = "public" if mode in {"public", "public_score"} else mode
        return self._engine.evaluator.evaluate(
            self._workspace,
            mode,
            self._engine.run_dir / "controller" / "evaluations" / self._attempt_id / directory_name,
        )

    def _ensure_candidate_safe(self, root: Path) -> None:
        self._engine._validate_candidate(root)
        changed = changed_files(self._parent.path, root)
        if changed:
            validate_edit_scope(changed, self._engine.editable)

    def _scope(self) -> Mapping[str, object]:
        return {
            "phase": "explore",
            "iteration": self._iteration,
            "island": self._island,
            "attempt_id": self._attempt_id,
        }


def _direction_assignments(artifact: object) -> dict[int, dict[str, object]]:
    if artifact is None:
        return {}
    path = Path(str(artifact))
    if not path.is_file():
        return {}
    payload = read_json_object(path)
    cards = {
        str(card.get("card_id")): dict(card)
        for card in payload.get("cards", [])
        if isinstance(card, Mapping) and card.get("card_id")
    }
    assignments: dict[int, dict[str, object]] = {}
    raw_assignment = payload.get("assignment", {})
    if not isinstance(raw_assignment, Mapping):
        return assignments
    for island, card_id in raw_assignment.items():
        if card_id is None or str(card_id) not in cards:
            continue
        assignments[int(island)] = cards[str(card_id)]
    return assignments


def _guidance_id(direction: Mapping[str, object] | None) -> str | None:
    return str(direction["card_id"]) if direction and direction.get("card_id") else None


def _safe_identifier(value: str, label: str) -> str:
    if not value or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _copy_role_input(source: Path, destination: Path) -> None:
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(f"Role input does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"Duplicate role input destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise ValueError(f"Role input is not a regular file or directory: {source}")


def _redact_role_input(path: Path, replacements: Mapping[str, str]) -> None:
    candidates = [path] if path.is_file() else list(path.rglob("*"))
    text_suffixes = {".json", ".jsonl", ".md", ".txt", ".log", ".diff", ".yaml", ".yml"}
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() not in text_suffixes:
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for before, after in replacements.items():
            text = text.replace(before, after)
        candidate.write_text(text, encoding="utf-8")


def _require_inside(path: Path, root: Path) -> None:
    root = root.resolve()
    path = path.resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"Research artifact escapes the run directory: {path}")


def _validate_patch_paths(path: Path) -> None:
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t", 1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        safe_relative_path(raw)


def _apply_patch(root: Path, patch: Path) -> None:
    attempts = ((), ("-p0",), ("-p1",))
    diagnostics: list[str] = []
    for strip in attempts:
        check = subprocess.run(
            ["git", "-C", str(root), "apply", "--check", *strip, str(patch)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if check.returncode != 0:
            diagnostics.append(check.stdout.strip())
            continue
        applied = subprocess.run(
            ["git", "-C", str(root), "apply", "--whitespace=nowarn", *strip, str(patch)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if applied.returncode == 0:
            return
        diagnostics.append(applied.stdout.strip())
    detail = next((item for item in diagnostics if item), "patch did not apply")
    raise ValueError(f"Patch conflict: {detail[:1000]}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, default=str) + "\n")


__all__: list[str] = []
