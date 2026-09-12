"""Controller-owned variant identities and bounded workflow capabilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from optiprofiler_evolve.agents.contracts import AgentJob, AgentJobResult

if TYPE_CHECKING:
    from optiprofiler_evolve.models import EvaluationResult


@dataclass(frozen=True)
class VariantRequest:
    """A complete-tree or patch-based solver variant materialization request."""

    variant_id: str
    base: Path
    patches: tuple[Path, ...] = ()
    tree: Path | None = None
    expected_base_hash: str | None = None


@dataclass(frozen=True)
class VariantHandle:
    """Controller-owned identity for a gated immutable solver variant."""

    variant_id: str
    path: Path
    tree_hash: str
    base_tree_hash: str
    change_hashes: tuple[str, ...]


class PublicVariantEvaluator(Protocol):
    """Evaluate one gated variant without exposing the evaluator itself."""

    def __call__(
        self,
        handle: VariantHandle,
        label: str,
        *,
        reference: str | None = None,
        mode: str = "public_score",
    ) -> EvaluationResult: ...


@dataclass(frozen=True)
class ControllerServices:
    """The complete capability budget available to trusted workflow phases.

    Keep this surface capped at five operations. New methods require an
    architecture-constitution change and matching isolation tests.
    """

    run_trusted_agent: Callable[[AgentJob], AgentJobResult]
    materialize_variant: Callable[[VariantRequest], VariantHandle]
    evaluate_public_variant: PublicVariantEvaluator
    select_by_validation: Callable[[Sequence[str], int], tuple[str, ...]]
    register_finalist: Callable[[VariantHandle, Mapping[str, object]], str]
