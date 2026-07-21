from __future__ import annotations

import unittest
from pathlib import Path

from optiprofiler_evolve.config import ToolConfig, WorkerConfig, WorkersConfig
from optiprofiler_evolve.harness import build_harness_command


class HarnessCommandTests(unittest.TestCase):
    def test_claude_stream_json_is_verbose_and_web_tools_follow_config(self) -> None:
        worker = WorkerConfig(harness="claude", model="test-model")
        workers = WorkersConfig(pool=(worker,))

        enabled = build_harness_command(worker, workers, ToolConfig(), Path("/workspace"))
        self.assertIn("--verbose", enabled)
        self.assertEqual(enabled[enabled.index("--output-format") + 1], "stream-json")
        enabled_tools = enabled[enabled.index("--tools") + 1].split(",")
        self.assertIn("WebSearch", enabled_tools)
        self.assertIn("WebFetch", enabled_tools)

        disabled = build_harness_command(
            worker,
            workers,
            ToolConfig(web_search=False),
            Path("/workspace"),
        )
        disabled_tools = disabled[disabled.index("--tools") + 1].split(",")
        self.assertNotIn("WebSearch", disabled_tools)
        self.assertNotIn("WebFetch", disabled_tools)

        no_shell = build_harness_command(
            worker,
            workers,
            ToolConfig(shell=False),
            Path("/workspace"),
        )
        no_shell_tools = no_shell[no_shell.index("--tools") + 1].split(",")
        self.assertNotIn("Bash", no_shell_tools)

    def test_codex_search_flag_follows_config(self) -> None:
        worker = WorkerConfig(harness="codex", model="test-model")
        workers = WorkersConfig(pool=(worker,))

        enabled = build_harness_command(worker, workers, ToolConfig(), Path("/workspace"))
        self.assertIn("--search", enabled)

        disabled = build_harness_command(
            worker,
            workers,
            ToolConfig(web_search=False),
            Path("/workspace"),
        )
        self.assertNotIn("--search", disabled)
        self.assertIn('web_search="disabled"', disabled)

    def test_harnesses_run_real_noninteractive_agent_loops(self) -> None:
        codex_worker = WorkerConfig(harness="codex", model="test-model")
        codex = build_harness_command(
            codex_worker,
            WorkersConfig(pool=(codex_worker,)),
            ToolConfig(),
            Path("/workspace"),
        )
        self.assertIn("exec", codex)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertIn("--json", codex)
        self.assertEqual(codex[-1], "-")

        claude_worker = WorkerConfig(harness="claude", model="test-model")
        claude = build_harness_command(
            claude_worker,
            WorkersConfig(pool=(claude_worker,)),
            ToolConfig(),
            Path("/workspace"),
        )
        self.assertIn("--print", claude)
        self.assertIn("--permission-mode", claude)
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "bypassPermissions")
        self.assertIn("--tools", claude)


if __name__ == "__main__":
    unittest.main()
