"""The prepare workflow stage."""

from __future__ import annotations

from optiprofiler_evolve.evaluation.references import materialize_reference
from optiprofiler_evolve.observability.fingerprints import build_experiment_fingerprints
from optiprofiler_evolve.quality.complexity import ComplexityChecker
from optiprofiler_evolve.shared.data import resolve_data_plan, write_data_manifests
from optiprofiler_evolve.shared.files import write_json as _write_json
from optiprofiler_evolve.shared.models import CandidateRecord
from optiprofiler_evolve.shared.solver import (
    copy_initial_source,
    tree_hash,
)

from .context import PrepareContext, PrepareResult


class PreparePhase:
    """Execute the prepare stage through its bounded controller context."""

    name = "prepare"
    requires = frozenset([])
    provides = frozenset(["prepared"])

    def run(self, context: PrepareContext) -> PrepareResult:
        complexity = None
        data = resolve_data_plan(context.config.data)
        ptype_value = data.selection.get("ptype")
        ptype = str(ptype_value) if ptype_value in {"u", "b", "l", "n"} else None
        reference = materialize_reference(
            initial=context.initial,
            destination=context.run_dir / "controller" / "reference",
            interface=context.interface,
            config=context.config.evaluation,
            ptype=ptype,
        )
        seed_path = context.run_dir / "candidates" / "seed"
        copy_initial_source(context.initial, seed_path)
        context.validate_candidate(seed_path)
        if context.config.complexity is not None:
            if context.runtime != "python":
                raise ValueError("Complexity checks support Python solvers only.")
            complexity = ComplexityChecker(
                seed_path,
                context.config.complexity,
                context.config.sandbox,
                context.run_dir / "controller" / "complexity",
            )
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
                    context.config.evaluation.forbidden_candidate_imports
                ),
            },
        )
        evaluator = context.evaluator_factory(
            runtime=context.runtime,
            reference=reference,
            interface=context.interface,
            data=data,
            config=context.config.evaluation,
        )
        seed_public_mode = (
            "public" if context.config.evaluation.feedback_mode != "score_only" else "public_score"
        )
        seed_public = evaluator.evaluate(
            seed_path,
            seed_public_mode,
            context.run_dir / "controller" / "evaluations" / "seed" / "public",
        )
        if not seed_public.success:
            raise RuntimeError(f"Initial solver evaluation failed: {seed_public.error}")
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
        )
        artifacts = {
            "prepared": {
                "seed": str(seed_path),
                "seed_public_evidence": str(seed_public.output_dir),
                "solver_contract": str(solver_contract),
                "islands": context.config.evolution.islands,
                "interface": f"{context.interface.file}:{context.interface.function}",
                "runtime": context.runtime,
                "editable": list(context.editable),
            }
        }

        return PrepareResult(
            artifacts=artifacts,
            data=data,
            reference=reference,
            evaluator=evaluator,
            complexity=complexity,
            seed=seed,
        )
