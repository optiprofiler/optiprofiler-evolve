"""Prepare immutable solver, data and initial assessment."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from optiprofiler_evolve.agents.feedback import publish_feedback
from optiprofiler_evolve.config import EvolveConfig
from optiprofiler_evolve.criteria.performance.contracts import Evaluator
from optiprofiler_evolve.criteria.performance.references import materialize_reference
from optiprofiler_evolve.data import DataPlan, resolve_data_plan, write_data_manifests
from optiprofiler_evolve.files import write_json as _write_json
from optiprofiler_evolve.models import CandidateRecord
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult
from optiprofiler_evolve.solver import InterfaceSpec, copy_initial_source, tree_hash


@dataclass(frozen=True)
class PreparedArtifact:
    """Public seed inputs consumed by research phases; no evaluator or private split."""

    seed: Path
    seed_public_evidence: Path
    solver_contract: Path
    islands: int


@dataclass(frozen=True)
class ExperimentFingerprints:
    """Independent identities for data, split, configuration, and solver code."""

    universe_fingerprint: str
    split_fingerprint: str
    config_fingerprint: str
    code_fingerprint: str
    experiment_fingerprint: str
    schema_version: str = "experiment_fingerprints/1"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def build_experiment_fingerprints(
    *,
    config: EvolveConfig,
    data: DataPlan,
    source_tree: Path,
    interface: InterfaceSpec,
    runtime: str,
    editable: Sequence[str],
) -> ExperimentFingerprints:
    """Build independent hashes without exposing credentials or hidden names."""

    if not data.universe_fingerprint or not data.split_fingerprint:
        raise ValueError("Resolved data must provide universe and split fingerprints.")
    config_fingerprint = canonical_fingerprint(config.resolved_dict())
    code_fingerprint = canonical_fingerprint(
        {
            "source_tree_hash": tree_hash(source_tree),
            "interface": {
                "file": interface.file,
                "function": interface.function,
            },
            "runtime": runtime,
            "editable": list(editable),
        }
    )
    identities = {
        "universe_fingerprint": data.universe_fingerprint,
        "split_fingerprint": data.split_fingerprint,
        "config_fingerprint": config_fingerprint,
        "code_fingerprint": code_fingerprint,
    }
    return ExperimentFingerprints(
        **identities,
        experiment_fingerprint=canonical_fingerprint(identities),
    )


def canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash a JSON-compatible mapping with stable ordering."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PrepareContext(PhaseContext):
    initial: Path
    interface: InterfaceSpec
    runtime: str
    editable: tuple[str, ...]
    config: EvolveConfig
    create_assessor: Callable[..., Evaluator]
    validate_candidate: Callable[[Path], None]


@dataclass(frozen=True, kw_only=True)
class PrepareResult(PhaseResult):
    artifacts: Mapping[str, object] = field(init=False)
    prepared: PreparedArtifact
    data: DataPlan
    reference: Path | None
    evaluator: Evaluator
    seed: CandidateRecord

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", {"prepared": self.prepared})
        super().__post_init__()


class PreparePhase:
    """Execute the prepare stage through its bounded controller context."""

    name = "prepare"
    requires = frozenset([])
    provides = frozenset(["prepared"])

    def run(self, context: PrepareContext) -> PrepareResult:
        enabled = context.config.assessment.enabled("performance")
        data = resolve_data_plan(context.config.data) if enabled else DataPlan(
            "disabled", {}, (), (), (), (), (), 0, "performance-disabled",
            universe_fingerprint="disabled", split_fingerprint="disabled",
        )
        ptype_value = data.selection.get("ptype")
        ptype = str(ptype_value) if ptype_value in {"u", "b", "l", "n"} else None
        reference = materialize_reference(
            initial=context.initial,
            destination=context.run_dir / "controller" / "reference",
            interface=context.interface,
            config=context.config.evaluation_config(next(
                profile for profile in ("smoke", "public", "validation", "hidden")
                if context.config.assessment.enabled("performance", profile)
            )),
            ptype=ptype,
        ) if enabled else None
        seed_path = context.run_dir / "candidates" / "seed"
        copy_initial_source(context.initial, seed_path)
        context.validate_candidate(seed_path)
        write_data_manifests(data, context.run_dir)
        fingerprints = build_experiment_fingerprints(
            config=context.config,
            data=data,
            source_tree=seed_path,
            interface=context.interface,
            runtime=context.runtime,
            editable=context.editable,
        )
        _write_json(
            context.run_dir / "controller" / "experiment_fingerprints.json",
            fingerprints.as_dict(),
        )
        solver_contract = context.run_dir / "solver_contract.json"
        _write_json(
            solver_contract,
            {
                "source": str(context.initial),
                "interface": f"{context.interface.file}:{context.interface.function}",
                "runtime": context.runtime,
                "editable": list(context.editable),
                "forbidden_candidate_imports": list(
                    context.config.execution.forbidden_candidate_imports
                ),
            },
        )
        evaluator = context.create_assessor(seed=seed_path, reference=reference, data=data)
        seed_public_mode = (
            "public" if context.config.execution.feedback_mode != "score_only" else "public_score"
        )
        seed_public = evaluator.evaluate(
            seed_path,
            seed_public_mode,
            context.run_dir / "controller" / "evaluations" / "seed" / "public",
        )
        if not seed_public.success:
            raise RuntimeError(f"Initial solver evaluation failed: {seed_public.error}")
        feedback = publish_feedback(
            seed_public, context.run_dir / "controller" / "feedback" / "seed",
            mode=context.config.execution.feedback_mode, emit=context.emit,
        )
        seed_validation = evaluator.evaluate(
            seed_path,
            "validation",
            context.run_dir / "controller" / "evaluations" / "seed" / "validation",
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
            selection_metrics=seed_validation.metric_bundle,
            assessment_score=seed_validation.assessment_score,
            assessment=seed_validation.assessment,
            public_evidence=feedback.output_dir,
        )
        prepared = PreparedArtifact(
            seed=seed_path,
            seed_public_evidence=feedback.output_dir,
            solver_contract=solver_contract,
            islands=context.config.evolution.islands,
        )

        return PrepareResult(
            prepared=prepared,
            data=data,
            reference=reference,
            evaluator=evaluator,
            seed=seed,
        )
