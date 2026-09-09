"""Idempotent registration of package-owned components."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from optiprofiler_evolve.evaluation.evaluation import create_evaluator
from optiprofiler_evolve.phases.prepare.phase import PreparePhase
from optiprofiler_evolve.phases.explore.phase import ExplorePhase
from optiprofiler_evolve.phases.validate.phase import ValidatePhase
from optiprofiler_evolve.phases.hidden.phase import HiddenPhase
from optiprofiler_evolve.phases.report.phase import ReportPhase
from optiprofiler_evolve.phases.challenger.phase import ChallengerPhase
from optiprofiler_evolve.phases.direction_scout.phase import DirectionScoutPhase
from optiprofiler_evolve.phases.recombine.phase import RecombinePhase
from optiprofiler_evolve.phases.strategy_analysis.phase import StrategyAnalysisPhase
from optiprofiler_evolve.phases.strategy_transfer.phase import StrategyTransferPhase
from optiprofiler_evolve.phases.explore.policies.builtin import EarlyStopPolicy, MigrationPolicy
from optiprofiler_evolve.controller.registry import register, registered
from optiprofiler_evolve.quality.review import AgentIntegrityReviewer, UnsafeApproveReviewer
from optiprofiler_evolve.controller.selection import (
    MetricParetoRetention,
    TopBiasedValidationWeightedSampler,
    ValidationLexicographicRetention,
)
from optiprofiler_evolve.phases.explore.steps.builtin import (
    FeedbackStep,
    MutateStep,
    PublicEvaluateStep,
    SmokeStep,
    StaticAuditStep,
)
from optiprofiler_evolve.agents.workers import CliWorkerAdapter


def register_builtin_components() -> None:
    """Populate the explicit local registries exactly once."""

    _register("step", "mutate", MutateStep)
    _register("step", "static_audit", StaticAuditStep)
    _register("step", "smoke", SmokeStep)
    _register("step", "public_evaluate", PublicEvaluateStep)
    _register("step", "feedback", FeedbackStep)
    _register("policy", "migration", MigrationPolicy)
    _register("policy", "early_stop", EarlyStopPolicy)
    _register("retention", "validation_lexicographic", ValidationLexicographicRetention)
    _register("retention", "metric_pareto", MetricParetoRetention)
    _register(
        "sampler",
        "top_biased_validation_weighted",
        TopBiasedValidationWeightedSampler,
    )
    _register("reviewer", "agent_integrity", AgentIntegrityReviewer)
    _register("reviewer", "unsafe_approve", UnsafeApproveReviewer)
    for phase in (PreparePhase, ExplorePhase, ValidatePhase, HiddenPhase, ReportPhase):
        _register("phase", phase.name, phase)
    _register("phase", "direction_scout", DirectionScoutPhase)
    _register("phase", "strategy_analysis", StrategyAnalysisPhase)
    _register("phase", "strategy_transfer", StrategyTransferPhase)
    _register("phase", "recombine", RecombinePhase)
    _register("phase", "challenger", ChallengerPhase)
    _register("worker", "cli", CliWorkerAdapter)
    _register("evaluator", "optiprofiler", create_evaluator)


def _register(kind: str, name: str, factory: Callable[..., Any]) -> None:
    if name not in registered(kind):
        register(kind, name, factory)


__all__: list[str] = []
