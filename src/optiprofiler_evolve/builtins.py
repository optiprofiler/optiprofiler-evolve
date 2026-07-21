"""Idempotent registration of package-owned components."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from .evaluation import create_evaluator
from .phases.builtin import CorePhase
from .phases.research import (
    ChallengerPhase,
    DirectionScoutPhase,
    RecombinePhase,
    StrategyAnalysisPhase,
)
from .policies.builtin import EarlyStopPolicy, MigrationPolicy
from .registry import register, registered
from .steps.builtin import (
    FeedbackStep,
    MutateStep,
    PublicEvaluateStep,
    SmokeStep,
    StaticAuditStep,
)
from .workers import CliWorkerAdapter


def register_builtin_components() -> None:
    """Populate the explicit local registries exactly once."""

    _register("step", "mutate", MutateStep)
    _register("step", "static_audit", StaticAuditStep)
    _register("step", "smoke", SmokeStep)
    _register("step", "public_evaluate", PublicEvaluateStep)
    _register("step", "feedback", FeedbackStep)
    _register("policy", "migration", MigrationPolicy)
    _register("policy", "early_stop", EarlyStopPolicy)
    for action in ("prepare", "explore", "validate", "hidden", "report"):
        _register("phase", action, partial(CorePhase, action=action))
    _register("phase", "direction_scout", DirectionScoutPhase)
    _register("phase", "strategy_analysis", StrategyAnalysisPhase)
    _register("phase", "recombine", RecombinePhase)
    _register("phase", "challenger", ChallengerPhase)
    _register("worker", "cli", CliWorkerAdapter)
    _register("evaluator", "optiprofiler", create_evaluator)


def _register(kind: str, name: str, factory: Callable[..., Any]) -> None:
    if name not in registered(kind):
        register(kind, name, factory)


__all__: list[str] = []
