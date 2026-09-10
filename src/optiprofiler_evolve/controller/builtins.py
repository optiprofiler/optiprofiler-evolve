"""Idempotent registration of package-owned components."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from optiprofiler_evolve.agents.workers import CliWorkerAdapter
from optiprofiler_evolve.controller.registry import register, registered
from optiprofiler_evolve.controller.selection import (
    MetricParetoRetention,
    TopBiasedValidationWeightedSampler,
    ValidationLexicographicRetention,
)
from optiprofiler_evolve.criteria.complexity.criterion import ComplexityCriterion
from optiprofiler_evolve.criteria.integrity.criterion import IntegrityCriterion
from optiprofiler_evolve.criteria.integrity.reviewer import (
    AgentIntegrityReviewer,
    UnsafeApproveReviewer,
)
from optiprofiler_evolve.criteria.performance.criterion import PerformanceCriterion
from optiprofiler_evolve.criteria.performance.evaluator import create_evaluator
from optiprofiler_evolve.phases.attempts.policies import EarlyStopPolicy, MigrationPolicy
from optiprofiler_evolve.phases.attempts.steps import (
    FeedbackStep,
    MutateStep,
    PublicEvaluateStep,
    SmokeStep,
    StaticAuditStep,
)
from optiprofiler_evolve.phases.challenger import ChallengerPhase
from optiprofiler_evolve.phases.direction_scout import DirectionScoutPhase
from optiprofiler_evolve.phases.explore import ExplorePhase
from optiprofiler_evolve.phases.hidden import HiddenPhase
from optiprofiler_evolve.phases.prepare import PreparePhase
from optiprofiler_evolve.phases.recombine import RecombinePhase
from optiprofiler_evolve.phases.report import ReportPhase
from optiprofiler_evolve.phases.strategy_analysis import StrategyAnalysisPhase
from optiprofiler_evolve.phases.strategy_transfer import StrategyTransferPhase
from optiprofiler_evolve.phases.validate import ValidatePhase


def register_builtin_components() -> None:
    """Populate the explicit local registries exactly once."""

    _register("criterion", "complexity", ComplexityCriterion)
    _register("criterion", "integrity", IntegrityCriterion)
    _register("criterion", "performance", PerformanceCriterion)
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
