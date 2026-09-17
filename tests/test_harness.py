from __future__ import annotations

import unittest
from pathlib import Path

from optiprofiler_evolve.agents.harness import build_harness_command
from optiprofiler_evolve.config import ToolConfig, WorkerConfig, WorkersConfig


class HarnessCommandTests(unittest.TestCase):
    def test_claude_stream_json_is_verbose_and_web_tools_follow_config(self) -> None:
        worker = WorkerConfig(harness="claude", model="test-model")
        workers = WorkersConfig(pool=(worker,))

        enabled = build_harness_command(
            worker,
            workers,
            ToolConfig(web_search=True),
            Path("/workspace"),
        )
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

    def test_claude_subscription_login_replaces_bare_mode_only_for_that_worker(self) -> None:
        api_key = WorkerConfig(harness="claude", model="m", env={"ANTHROPIC_API_KEY": "k"})
        login = WorkerConfig(harness="claude", model="m",
                             env={"CLAUDE_CREDENTIALS_JSON_PATH": "/owner/login.json"})
        passed = WorkerConfig(harness="claude", model="m",
                              pass_env=("CLAUDE_CREDENTIALS_JSON_PATH",))
        codex_named = WorkerConfig(harness="claude", model="m",
                                   env={"CODEX_AUTH_JSON_PATH": "/owner/auth.json"})
        build = lambda worker: build_harness_command(  # noqa: E731
            worker, WorkersConfig(pool=(worker,)), ToolConfig(), Path("/workspace"))
        for worker in (api_key, codex_named):
            self.assertIn("--bare", build(worker))
            self.assertNotIn("--setting-sources", build(worker))
        for worker in (login, passed):
            command = build(worker)
            # Bare mode never reads an OAuth login; project/local settings stay excluded.
            self.assertNotIn("--bare", command)
            self.assertEqual(command[command.index("--setting-sources") + 1], "user")
            self.assertIn("--strict-mcp-config", command)
            self.assertEqual(command[command.index("--model") + 1], "m")
            self.assertNotIn("--fallback-model", command)
            self.assertNotIn("/owner/login.json", command)

    def test_codex_search_flag_follows_config(self) -> None:
        worker = WorkerConfig(harness="codex", model="test-model")
        workers = WorkersConfig(pool=(worker,))

        enabled = build_harness_command(
            worker,
            workers,
            ToolConfig(web_search=True),
            Path("/workspace"),
        )
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
