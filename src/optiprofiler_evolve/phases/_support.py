"""Shared research phase inputs and tool configuration."""

from __future__ import annotations

from collections.abc import Mapping

from optiprofiler_evolve.config import EvolveConfig, ToolConfig
from optiprofiler_evolve.phases.contracts import PhaseContext
from optiprofiler_evolve.phases.prepare import PreparedArtifact


def _prepared(context: PhaseContext) -> PreparedArtifact:
    value = context.artifacts.get("prepared")
    if not isinstance(value, PreparedArtifact):
        raise RuntimeError("research phase requires the structured prepared artifact")
    return value


def _require_public_performance(config: EvolveConfig, phase: str) -> None:
    if not config.assessment.enabled("performance", "public"):
        raise ValueError(f"Phase {phase!r} requires public performance assessment.")


def _tool_config(
    value: Mapping[str, object] | None,
    *,
    network: bool,
    web_search: bool,
) -> ToolConfig:
    defaults: dict[str, object] = {
        "preset": "research",
        "network": network,
        "web_search": web_search,
        "shell": True,
        "python": True,
        "git": True,
        "compilers": False,
        "package_install": False,
        "communication": "none",
    }
    defaults.update(dict(value or {}))
    tools = ToolConfig(**defaults)
    tools.validate()
    return tools


def _tool_payload(tools: ToolConfig) -> dict[str, object]:
    return {
        "preset": tools.preset,
        "web_search": tools.web_search,
        "network": tools.network,
        "shell": tools.shell,
        "python": tools.python,
        "git": tools.git,
        "compilers": tools.compilers,
        "package_install": tools.package_install,
        "communication": tools.communication,
    }
