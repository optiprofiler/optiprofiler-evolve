"""Command adapters for supported coding-agent CLIs."""

from __future__ import annotations

from pathlib import Path

from .config import ToolConfig, WorkerConfig, WorkersConfig


def build_harness_command(
    worker: WorkerConfig,
    workers: WorkersConfig,
    tools: ToolConfig,
    workspace: Path,
) -> list[str]:
    """Build an argv-only command; Docker remains the actual security boundary."""

    if worker.harness == "codex":
        command = [
            "codex",
        ]
        if tools.web_search:
            command.append("--search")
        command.extend(
            [
                "exec",
                "--model",
                worker.model,
                "--cd",
                str(workspace),
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--ephemeral",
                "--json",
            ]
        )
        if not tools.web_search:
            command.extend(["--config", 'web_search="disabled"'])
        if worker.profile:
            command.extend(["--profile", worker.profile])
        command.extend(worker.args)
        command.append("-")
        return command

    if worker.harness == "claude":
        allowed = ["Read", "Edit", "Write", "Glob", "Grep"]
        if tools.shell:
            allowed.append("Bash")
        if tools.web_search:
            allowed.extend(["WebSearch", "WebFetch"])
        command = [
            "claude",
            "--print",
            "--model",
            worker.model,
            "--permission-mode",
            "bypassPermissions",
            "--no-session-persistence",
            "--bare",
            "--verbose",
            "--output-format",
            "stream-json",
            "--tools",
            ",".join(allowed),
        ]
        if workers.max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(workers.max_budget_usd)])
        command.extend(worker.args)
        return command

    raise ValueError(f"Unsupported worker harness: {worker.harness!r}")


__all__: list[str] = []
