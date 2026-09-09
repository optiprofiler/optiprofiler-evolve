"""Trusted benchmark execution contract."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from optiprofiler_evolve.models import EvaluationResult


class Evaluator(Protocol):
    """Trusted evaluation backend; the only component that resolves problem refs."""

    name: str
    deterministic: bool

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult: ...
