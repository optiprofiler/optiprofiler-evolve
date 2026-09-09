"""Shared research phase inputs and tool configuration."""

from __future__ import annotations

from collections.abc import Mapping

from optiprofiler_evolve.shared.config import ToolConfig
from optiprofiler_evolve.shared.protocols import (
    PhaseContext,
)


def _prepared(context: PhaseContext) -> Mapping[str, object]:
    value = context.artifacts.get("prepared")
    if not isinstance(value, Mapping):
        raise RuntimeError("research phase requires the structured prepared artifact")
    return value


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
