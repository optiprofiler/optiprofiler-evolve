"""Authoritative candidate, population and phase-result transitions."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from optiprofiler_evolve.config import EvolveConfig
from optiprofiler_evolve.controller.contracts import VariantHandle
from optiprofiler_evolve.controller.selection import (
    MetricSelectionError,
    ParentSampler,
    RetentionPolicy,
)
from optiprofiler_evolve.controller.state import RunState
from optiprofiler_evolve.files import append_json as _append_json
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import (
    Attempt,
    CandidateRecord,
    EvaluationResult,
)
from optiprofiler_evolve.observability.attempts import attempt_summary
from optiprofiler_evolve.phases.attempts.contracts import (
    PopulationEdit,
)
from optiprofiler_evolve.phases.contracts import PhaseResult
from optiprofiler_evolve.phases.hidden import HiddenResult
from optiprofiler_evolve.phases.prepare import PrepareResult
from optiprofiler_evolve.phases.validate import ValidateResult
from optiprofiler_evolve.solver import tree_hash


class RunCommits:
    """The sole population and phase-state writer."""

    def __init__(
        self, state: RunState, config: EvolveConfig, run_dir: Path,
        retention: RetentionPolicy, parent_sampler: ParentSampler,
        emit: Callable[..., None],
    ) -> None:
        self.state = state
        self.config = config
        self.run_dir = run_dir
        self.retention = retention
        self.parent_sampler = parent_sampler
        self._emit = emit

    def population_snapshot(self) -> tuple[tuple[CandidateRecord, ...], ...]:
        return tuple(tuple(population) for population in self.state.populations)

    def require_open_selection(self) -> None:
        if self.state.champion is not None:
            raise RuntimeError("Champion is fixed; candidate selection is closed.")

    def begin_hidden(self) -> None:
        if self.state.hidden_started:
            raise RuntimeError("Hidden evaluation has already been started.")
        selected = self.state.champion
        if selected is None:
            raise ValueError("Hidden evaluation requires the fixed champion.")
        if tree_hash(selected[1].path) != selected[1].tree_hash:
            raise ValueError("Champion changed after formal assessment.")
        # Consume before execution: a failed hidden evaluation must not permit another query.
        self.state.hidden_started = True

    def set_directions(self, directions: Mapping[int, dict[str, object]]) -> None:
        self.require_open_selection()
        self.state.direction_assignments = dict(directions)

    def commit_phase_result(self, result: PhaseResult) -> None:
        if result.status not in {"succeeded", "skipped"}:
            raise ValueError("Cannot commit a failed or cancelled phase result.")
        if isinstance(result, PrepareResult):
            self.require_open_selection()
            self.state.candidates[result.seed.candidate_id] = result.seed
            self.state.populations = [[result.seed] for _ in range(self.config.evolution.islands)]
            self.write_checkpoint(0)
        elif isinstance(result, ValidateResult):
            self.require_open_selection()
            _, champion = result.champion
            registered = self.state.candidates.get(champion.candidate_id)
            if registered is not champion or not champion.valid or champion.assessment_score is None:
                raise ValueError("Champion must be an admitted, formally assessed candidate.")
        elif isinstance(result, HiddenResult):
            if not self.state.hidden_started:
                raise RuntimeError("Hidden result requires an authorized hidden evaluation.")
            if self.state.final_result is not None:
                raise RuntimeError("Hidden result has already been committed.")
        self.state.phase_results.append(result)

    def record_variant(self, handle: VariantHandle, base: Path) -> None:
        self.require_open_selection()
        self.state.variant_handles[handle.variant_id] = handle
        self.state.variant_bases[handle.variant_id] = base

    def record_public_evaluation(self, candidate_id: str, result: EvaluationResult) -> None:
        self.require_open_selection()
        self.state.variant_public[candidate_id] = result

    def record_validation_evaluation(self, candidate_id: str, result: EvaluationResult) -> None:
        self.require_open_selection()
        self.state.variant_validation[candidate_id] = result
        self.state.validation_query_count += 1

    def begin_validation_selection(self) -> None:
        self.require_open_selection()
        self.state.validation_selection_count += 1

    def register_research_finalist(self, record: CandidateRecord) -> None:
        self.require_open_selection()
        self.state.candidates[record.candidate_id] = record
        self.state.research_finalists[record.candidate_id] = record

    def accept_and_record(self, attempt: Attempt) -> None:
        self.require_open_selection()
        record = attempt.record
        accepted = False
        if record.valid:
            population = [*self.state.populations[record.island], record]
            try:
                retained = self.trim_population(population)
            except MetricSelectionError as exc:
                record = replace(
                    record,
                    valid=False,
                    error=f"metric_incomplete: {exc}",
                )
            else:
                self.state.populations[record.island] = retained
                accepted = record.candidate_id in {item.candidate_id for item in retained}
        self.state.candidates[record.candidate_id] = record
        summary = attempt_summary(attempt, record)
        self.state.attempt_history.append(summary)
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

    def apply_population_edit(self, edit: PopulationEdit) -> None:
        self.require_open_selection()
        if edit.budget:
            raise NotImplementedError(
                "PopulationEdit.budget is reserved for a future scheduler and cannot "
                "be used until the engine has an explicit budget consumer."
            )
        killed = set(edit.kill)
        populations = [
            [record for record in population if record.candidate_id not in killed]
            for population in self.state.populations
        ]
        for candidate_id, target in edit.migrate:
            if not 0 <= target < len(populations):
                raise ValueError(f"Migration target island is out of range: {target}")
            try:
                record = self.state.candidates[candidate_id]
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
            populations[target] = self.trim_population([*populations[target], record])
        self.state.populations = populations

    def select_parent(
        self, population: list[CandidateRecord], rng: random.Random
    ) -> CandidateRecord:
        ordered = self.trim_population(population)
        selected = self.parent_sampler.select(tuple(ordered), rng)
        allowed = {record.candidate_id: record for record in ordered}
        if selected.candidate_id not in allowed:
            raise RuntimeError("Parent sampler returned a candidate outside the island archive.")
        return allowed[selected.candidate_id]

    def trim_population(self, population: list[CandidateRecord]) -> list[CandidateRecord]:
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

    def fixed_finalists(
        self, populations: list[list[CandidateRecord]]
    ) -> list[tuple[int, CandidateRecord]]:
        fixed: list[tuple[int, CandidateRecord]] = []
        seen: set[str] = set()
        count = self.config.evolution.finalists_per_island
        for island, population in enumerate(populations):
            for record in self.trim_population(population)[:count]:
                if record.candidate_id not in seen:
                    fixed.append((island, record))
                    seen.add(record.candidate_id)
        return fixed

    def write_checkpoint(self, iteration: int) -> None:
        payload = {
            "iteration": iteration,
            "populations": [
                [record.candidate_id for record in population]
                for population in self.state.populations
            ],
            "candidates": {
                key: value.as_dict() for key, value in sorted(self.state.candidates.items())
            },
        }
        _write_json(self.run_dir / "checkpoints" / f"iteration_{iteration:03d}.json", payload)
        _write_json(self.run_dir / "state.json", payload)
