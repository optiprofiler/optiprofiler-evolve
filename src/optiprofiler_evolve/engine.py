"""Population controller for benchmark-driven solver evolution."""

from __future__ import annotations

import concurrent.futures
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .broker import EvaluationBroker
from .config import EvolveConfig, WorkerConfig
from .data import DataPlan, resolve_data_plan, write_data_manifests
from .evaluation import Evaluator, create_evaluator
from .models import CandidateRecord, EvolveResult, FinalistResult
from .prompt import build_worker_prompt
from .references import materialize_reference
from .sandbox import AgentRunResult, run_agent
from .solver import (
    InterfaceSpec,
    changed_files,
    copy_initial_source,
    copy_solver_tree,
    tree_hash,
    validate_edit_scope,
    validate_interface,
    validate_tree_safety,
)


AgentRunner = Callable[..., AgentRunResult]


@dataclass(frozen=True)
class _Attempt:
    record: CandidateRecord
    changed: tuple[str, ...]
    returncode: int
    transcript: Path


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
        agent_runner: AgentRunner = run_agent,
        evaluator_factory: Callable[..., Evaluator] = create_evaluator,
    ) -> None:
        self.initial = Path(initial).expanduser().resolve()
        self.interface = interface
        self.runtime = runtime
        self.editable = tuple(editable)
        self.config = config
        self.run_dir = run_dir.expanduser().resolve()
        self.agent_runner = agent_runner
        self.evaluator_factory = evaluator_factory
        self.rng = random.Random(config.evolution.random_seed)
        self.attempt_history: list[dict[str, Any]] = []
        self.candidates: dict[str, CandidateRecord] = {}

    def run(self) -> EvolveResult:
        self._initialize_run_directory()
        reference = materialize_reference(
            initial=self.initial,
            destination=self.run_dir / "controller" / "reference",
            interface=self.interface,
            config=self.config.evaluation,
        )
        seed_path = self.run_dir / "candidates" / "seed"
        copy_initial_source(self.initial, seed_path)
        self._validate_candidate(seed_path)

        data = resolve_data_plan(self.config.data)
        write_data_manifests(data, self.run_dir)
        _write_json(self.run_dir / "resolved_config.json", self.config.redacted_dict())
        _write_json(
            self.run_dir / "solver_contract.json",
            {
                "source": str(self.initial),
                "interface": f"{self.interface.file}:{self.interface.function}",
                "runtime": self.runtime,
                "editable": list(self.editable),
            },
        )

        evaluator = self.evaluator_factory(
            runtime=self.runtime,
            reference=reference,
            interface=self.interface,
            data=data,
            config=self.config.evaluation,
        )
        seed_eval = evaluator.evaluate(
            seed_path, "public", self.run_dir / "controller" / "evaluations" / "seed" / "public"
        )
        if not seed_eval.success:
            raise RuntimeError(f"Initial solver evaluation failed: {seed_eval.error}")
        seed_validation = evaluator.evaluate(
            seed_path,
            "validation",
            self.run_dir / "controller" / "evaluations" / "seed" / "validation",
        )
        if not seed_validation.success:
            raise RuntimeError(f"Initial solver validation failed: {seed_validation.error}")
        seed = CandidateRecord(
            candidate_id="seed",
            island=-1,
            generation=0,
            parent_id=None,
            path=seed_path,
            tree_hash=tree_hash(seed_path),
            public_score=seed_eval.score,
            validation_score=seed_validation.score,
        )
        self.candidates[seed.candidate_id] = seed
        populations = [[seed] for _ in range(self.config.evolution.islands)]
        self._write_checkpoint(0, populations)

        for generation in range(1, self.config.evolution.rounds + 1):
            jobs: list[tuple[int, CandidateRecord, WorkerConfig]] = []
            for island, population in enumerate(populations):
                jobs.append((island, self._select_parent(population), self._select_worker()))

            attempts: list[_Attempt] = []
            max_workers = min(self.config.workers.max_parallel, len(jobs))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        self._run_attempt,
                        generation=generation,
                        island=island,
                        parent=parent,
                        worker=worker,
                        evaluator=evaluator,
                        data=data,
                    ): (island, parent, worker)
                    for island, parent, worker in jobs
                }
                for future in concurrent.futures.as_completed(futures):
                    island, parent, worker = futures[future]
                    try:
                        attempts.append(future.result())
                    except Exception as exc:
                        candidate_id = f"g{generation:03d}-i{island:02d}"
                        transcript = self.run_dir / "transcripts" / f"{candidate_id}.jsonl"
                        transcript.parent.mkdir(parents=True, exist_ok=True)
                        transcript.write_text(
                            f"[controller] attempt setup failed: {type(exc).__name__}: {exc}\n",
                            encoding="utf-8",
                        )
                        record = CandidateRecord(
                            candidate_id=candidate_id,
                            island=island,
                            generation=generation,
                            parent_id=parent.candidate_id,
                            path=self.run_dir / "workspaces" / candidate_id,
                            tree_hash="",
                            public_score=0.0,
                            validation_score=0.0,
                            worker=f"{worker.harness}:{worker.model}",
                            valid=False,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        attempts.append(_Attempt(record, (), 1, transcript))

            for attempt in sorted(attempts, key=lambda item: item.record.island):
                record = attempt.record
                self.candidates[record.candidate_id] = record
                summary = {
                    "candidate_id": record.candidate_id,
                    "island": record.island,
                    "generation": record.generation,
                    "parent_id": record.parent_id,
                    "public_score": record.public_score,
                    "validation_score": record.validation_score,
                    "valid": record.valid,
                    "error": record.error,
                    "changed_files": list(attempt.changed),
                    "worker_returncode": attempt.returncode,
                    "transcript": str(attempt.transcript),
                }
                self.attempt_history.append(summary)
                _append_json(self.run_dir / "attempts.jsonl", summary)
                if record.valid:
                    population = populations[record.island]
                    population.append(record)
                    populations[record.island] = self._trim_population(population)

            interval = self.config.evolution.migration_interval
            if interval and generation % interval == 0:
                populations = self._migrate(populations)
            self._write_checkpoint(generation, populations)

        validation_finalists = self._fixed_finalists(populations)
        champion_island, champion = max(
            validation_finalists,
            key=lambda item: (
                item[1].validation_score,
                item[1].public_score,
                item[1].generation,
                item[1].candidate_id,
            ),
        )
        best_final = self._evaluate_hidden_champion(champion_island, champion, evaluator, data)
        final_results = [best_final]
        if not best_final.success:
            raise RuntimeError(f"Controller-only hidden evaluation failed: {best_final.error}")
        best_record = self.candidates[best_final.candidate_id]
        final_solver = self.run_dir / "final_solver"
        copy_solver_tree(best_record.path, final_solver)
        _write_json(
            self.run_dir / "final_ranking.json",
            [best_final.as_dict()],
        )
        self._write_final_report(best_final, final_results, final_solver)
        return EvolveResult(
            run_dir=self.run_dir,
            best_solver=final_solver,
            best_candidate_id=best_final.candidate_id,
            public_score=best_final.public_score,
            validation_score=best_final.validation_score,
            final_score=best_final.final_score,
            finalists=tuple(final_results),
        )

    def _run_attempt(
        self,
        *,
        generation: int,
        island: int,
        parent: CandidateRecord,
        worker: WorkerConfig,
        evaluator: Evaluator,
        data: DataPlan,
    ) -> _Attempt:
        candidate_id = f"g{generation:03d}-i{island:02d}"
        workspace = self.run_dir / "workspaces" / candidate_id
        copy_solver_tree(parent.path, workspace)
        tools_dir = self.run_dir / "controller" / "worker_tools" / candidate_id
        transcript = self.run_dir / "transcripts" / f"{candidate_id}.jsonl"
        broker = EvaluationBroker(
            workspace=workspace,
            control_dir=self.run_dir / "controller" / "brokers" / candidate_id,
            evaluator=evaluator,
            max_smoke_calls=self.config.evaluation.max_smoke_calls_per_worker,
            max_public_calls=self.config.evaluation.max_public_calls_per_worker,
            candidate_validator=self._validate_candidate,
        )
        broker.install_tools(tools_dir)
        connection = broker.start(docker=self.config.sandbox.backend == "docker")
        prompt = build_worker_prompt(
            interface=self.interface,
            runtime=self.runtime,
            editable=self.editable,
            data=data,
            generation=generation,
            island=island,
            parent_score=parent.public_score,
            controller_memory=self._memory_for(island),
            token_budget=self.config.workers.token_budget,
        )
        run_result = AgentRunResult(1, transcript)
        changed: tuple[str, ...] = ()
        try:
            run_result = self.agent_runner(
                worker=worker,
                workers=self.config.workers,
                sandbox=self.config.sandbox,
                workspace=workspace,
                tools_dir=tools_dir,
                broker=connection,
                prompt=prompt,
                transcript=transcript,
            )
        except Exception as exc:
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(f"[controller] worker launch failed: {exc}\n", encoding="utf-8")
        finally:
            broker.stop()

        try:
            self._validate_candidate(workspace)
            changed = changed_files(parent.path, workspace)
            if not changed:
                raise ValueError("Worker produced no solver changes.")
            validate_edit_scope(changed, self.editable)
            canonical = evaluator.evaluate(
                workspace,
                "public",
                self.run_dir / "controller" / "evaluations" / candidate_id / "public",
            )
            if not canonical.success:
                raise RuntimeError(canonical.error or "canonical public evaluation failed")
            validation = evaluator.evaluate(
                workspace,
                "validation",
                self.run_dir
                / "controller"
                / "evaluations"
                / candidate_id
                / "validation",
            )
            if not validation.success:
                raise RuntimeError(validation.error or "controller validation evaluation failed")
            snapshot = self.run_dir / "candidates" / candidate_id
            copy_solver_tree(workspace, snapshot)
            digest = tree_hash(snapshot)
            record = CandidateRecord(
                candidate_id=candidate_id,
                island=island,
                generation=generation,
                parent_id=parent.candidate_id,
                path=snapshot,
                tree_hash=digest,
                public_score=canonical.score,
                validation_score=validation.score,
                worker=f"{worker.harness}:{worker.model}",
            )
        except Exception as exc:
            record = CandidateRecord(
                candidate_id=candidate_id,
                island=island,
                generation=generation,
                parent_id=parent.candidate_id,
                path=workspace,
                tree_hash="",
                public_score=0.0,
                validation_score=0.0,
                worker=f"{worker.harness}:{worker.model}",
                valid=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return _Attempt(record, changed, run_result.returncode, transcript)

    def _validate_candidate(self, root: Path) -> None:
        validate_tree_safety(
            root,
            max_files=self.config.sandbox.max_candidate_files,
            max_bytes=self.config.sandbox.max_candidate_bytes,
        )
        validate_interface(root, self.interface, self.runtime)

    def _select_parent(self, population: list[CandidateRecord]) -> CandidateRecord:
        ordered = self._trim_population(population)
        if len(ordered) == 1 or self.rng.random() < 0.7:
            return ordered[0]
        weights = [max(record.validation_score, 1e-6) for record in ordered]
        return self.rng.choices(ordered, weights=weights, k=1)[0]

    def _select_worker(self) -> WorkerConfig:
        weighted = [worker for worker in self.config.workers.pool for _ in range(worker.weight)]
        return self.rng.choice(weighted)

    def _trim_population(self, population: list[CandidateRecord]) -> list[CandidateRecord]:
        unique = {record.candidate_id: record for record in population}
        ordered = sorted(
            unique.values(),
            key=lambda record: (
                -record.validation_score,
                -record.public_score,
                -record.generation,
                record.candidate_id,
            ),
        )
        return ordered[: self.config.evolution.population_per_island]

    def _migrate(self, populations: list[list[CandidateRecord]]) -> list[list[CandidateRecord]]:
        champions = [self._trim_population(population)[0] for population in populations]
        migrated: list[list[CandidateRecord]] = []
        for island, population in enumerate(populations):
            incoming = champions[(island - 1) % len(champions)]
            migrated.append(self._trim_population(population + [incoming]))
        return migrated

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
            successful = sorted(
                (item for item in history if item["valid"]),
                key=lambda item: item["public_score"],
                reverse=True,
            )[:5]
            failed = [item for item in history if not item["valid"]][-2:]
            history = successful + failed
        else:
            history = history[-10:]
        worker_visible = [
            {
                key: value
                for key, value in item.items()
                if key not in {"validation_score"}
            }
            for item in history
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

    def _write_checkpoint(self, generation: int, populations: list[list[CandidateRecord]]) -> None:
        payload = {
            "generation": generation,
            "populations": [
                [record.candidate_id for record in population] for population in populations
            ],
            "candidates": {key: value.as_dict() for key, value in sorted(self.candidates.items())},
        }
        _write_json(self.run_dir / "checkpoints" / f"generation_{generation:03d}.json", payload)
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
        (self.run_dir / "FINAL_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _append_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, default=str) + "\n")


__all__: list[str] = []
