"""Thin phase wrappers around engine-owned core actions."""

from __future__ import annotations

from ..protocols import PhaseContext, PhaseResult


_CONTRACTS = {
    "prepare": (frozenset(), frozenset({"prepared"})),
    "explore": (frozenset({"prepared"}), frozenset({"populations"})),
    "validate": (frozenset({"populations"}), frozenset({"champion"})),
    "hidden": (frozenset({"champion"}), frozenset({"final"})),
    "report": (frozenset({"final"}), frozenset({"result", "report"})),
}


class CorePhase:
    """Invoke one named engine action while keeping phase order configurable."""

    def __init__(self, *, action: str) -> None:
        if action not in _CONTRACTS:
            raise ValueError(f"Unknown core phase action: {action!r}")
        self.name = action
        self.requires, self.provides = _CONTRACTS[action]

    def run(self, context: PhaseContext) -> PhaseResult:
        artifacts = context.invoke(self.name)
        missing = self.provides.difference(artifacts)
        if missing:
            raise RuntimeError(f"Core phase {self.name!r} did not provide {sorted(missing)!r}.")
        return PhaseResult(artifacts=artifacts)


__all__: list[str] = []
