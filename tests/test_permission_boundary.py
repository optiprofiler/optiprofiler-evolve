"""Private-mode helpers must never change directories they did not create.

Every permission change here goes through a guard that records the request and
only performs it inside the test's own temporary directory, so a regression can
never again reach ``/tmp`` or ``/`` (which broke a host when tests ran as root).
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.agents.provider_gateway import GatewayAuditLog
from optiprofiler_evolve.config import SandboxConfig, ToolConfig, WorkerConfig, WorkersConfig
from optiprofiler_evolve.observability.traces import prepare_trace

_REAL_CHMOD = Path.chmod


@contextmanager
def guarded_chmod(sandbox: Path):
    """Record every chmod; perform it only on paths strictly inside ``sandbox``."""
    requested: list[Path] = []
    sandbox = sandbox.resolve()

    def chmod(path: Path, mode: int, **kwargs) -> None:
        requested.append(Path(path))
        if sandbox in Path(path).resolve().parents:
            _REAL_CHMOD(path, mode, **kwargs)

    with patch.object(Path, "chmod", chmod):
        yield requested


def _prepare(root: Path) -> None:
    worker = WorkerConfig(harness="codex", model="test")
    prepare_trace(
        root=root, prompt="probe", command=["probe"], worker=worker,
        workers=WorkersConfig(pool=(worker,), tools=ToolConfig(network=False, web_search=False)),
        sandbox=SandboxConfig(backend="unsafe_local"),
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TraceParentBoundaryTests(unittest.TestCase):
    def test_trace_without_a_traces_ancestor_leaves_every_existing_parent_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            holder = sandbox / "evolve-test"
            holder.mkdir(mode=0o755)
            with guarded_chmod(sandbox) as requested:
                _prepare(holder / "trace")
            outside = [path for path in requested if holder / "trace" not in (path, *path.parents)]
            self.assertEqual(outside, [])  # previously: holder, the temp dir, /tmp and /
            self.assertEqual(_mode(holder), 0o755)
            self.assertEqual(_mode(holder / "trace"), 0o700)

    def test_nested_research_traces_stay_private_without_touching_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            research = sandbox / "run" / "research"
            research.mkdir(parents=True, mode=0o755)
            (sandbox / "run").chmod(0o755)
            root = research / "traces" / "strategy-analyst" / "analysis-island-0"
            with guarded_chmod(sandbox) as requested:
                _prepare(root)
                _prepare(research / "traces" / "strategy-analyst" / "analysis-island-1")
            for private in (research / "traces", root.parent, root, root / "input"):
                self.assertEqual(_mode(private), 0o700, private)
            for untouched in (research, sandbox / "run", sandbox):
                self.assertNotIn(untouched, requested)
            self.assertEqual((_mode(research), _mode(sandbox / "run")), (0o755, 0o755))

    def test_a_traces_directory_made_by_someone_else_is_not_claimed_by_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            foreign = sandbox / "shared" / "traces"
            foreign.mkdir(parents=True, mode=0o755)
            foreign.chmod(0o755)
            with guarded_chmod(sandbox) as requested:
                _prepare(foreign / "attempt")
            self.assertNotIn(foreign, requested)
            self.assertEqual(_mode(foreign), 0o755)
            self.assertEqual(_mode(foreign / "attempt"), 0o700)

    def test_symlinked_ancestor_cannot_redirect_hardening_to_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            target = sandbox / "elsewhere"
            target.mkdir(mode=0o755)
            target.chmod(0o755)
            (sandbox / "run").mkdir()
            (sandbox / "run" / "traces").symlink_to(target, target_is_directory=True)
            with guarded_chmod(sandbox) as requested:
                _prepare(sandbox / "run" / "traces" / "attempt")
            self.assertEqual(_mode(target), 0o755)
            self.assertFalse({target, sandbox / "run" / "traces"} & set(requested))


class SuiteGuardTests(unittest.TestCase):
    def test_pytest_runs_refuse_to_repermission_the_shared_temp_root(self) -> None:
        if getattr(os.chmod, "__module__", "") != "conftest":
            self.skipTest("the chmod guard is installed by tests/conftest.py under pytest")
        shared = Path(tempfile.gettempdir())
        unchanged = _mode(shared)  # requesting the current mode is harmless even unguarded
        with self.assertRaisesRegex(AssertionError, "protected directory"):
            shared.chmod(unchanged)
        with self.assertRaisesRegex(AssertionError, "protected directory"):
            os.chmod("/", _mode(Path("/")))


class GatewayAuditParentBoundaryTests(unittest.TestCase):
    def test_audit_log_beside_other_files_does_not_privatize_the_shared_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            shared = sandbox / "shared"
            shared.mkdir(mode=0o755)
            shared.chmod(0o755)
            with guarded_chmod(sandbox) as requested:
                log = GatewayAuditLog(shared / "gateway.jsonl")
                log.append({"event": "probe"})
            self.assertIsNone(log.failure)
            self.assertNotIn(shared, requested)  # e.g. ``--audit-log /tmp/gateway.jsonl``
            self.assertEqual(_mode(shared), 0o755)
            self.assertEqual(_mode(shared / "gateway.jsonl"), 0o600)

    def test_audit_directory_created_for_the_log_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            with guarded_chmod(sandbox):
                log = GatewayAuditLog(sandbox / "state" / "audit" / "gateway.jsonl")
                log.append({"event": "probe"})
            self.assertIsNone(log.failure)
            self.assertEqual(_mode(sandbox / "state" / "audit"), 0o700)
            self.assertEqual(_mode(sandbox / "state" / "audit" / "gateway.jsonl"), 0o600)


if __name__ == "__main__":
    unittest.main()
