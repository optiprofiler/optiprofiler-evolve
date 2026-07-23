from __future__ import annotations

import concurrent.futures
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from optiprofiler_evolve.config import (
    ProviderGatewayConfig,
    SandboxConfig,
    ToolConfig,
    WorkerConfig,
    WorkersConfig,
)
from optiprofiler_evolve.trace_ledger import TraceLedger, reconcile_trace_run
from optiprofiler_evolve.traces import (
    finalize_adapter_trace,
    prepare_trace,
    recover_incomplete_traces,
    render_transcript,
    run_captured_process,
)


class TraceCaptureTests(unittest.TestCase):
    def test_provider_route_and_credential_name_stay_out_of_worker_trace_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = WorkerConfig(
                harness="codex",
                model="test-model",
                env={"PRIVATE_PROVIDER_TOKEN": "secret-value"},
                provider_gateway=ProviderGatewayConfig(
                    upstream_base_url="https://private-provider.example/v1",
                    credential_env="PRIVATE_PROVIDER_TOKEN",
                ),
            )
            paths = prepare_trace(
                root=root / "trace",
                prompt="private prompt",
                command=["<worker-adapter>", "cli"],
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(),
                secret_values=worker.env,
            )

            resolved = (paths.input_dir / "resolved_worker.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("private-provider.example", resolved)
            self.assertNotIn("PRIVATE_PROVIDER_TOKEN", resolved)
            self.assertNotIn("secret-value", resolved)
            self.assertEqual(resolved.count("<controller-owned>"), 2)

    def test_raw_streams_are_private_incremental_and_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = WorkerConfig(
                harness="codex",
                model="test-model",
                env={"OPENAI_API_KEY": "secret-value", "OPENAI_BASE_URL": "https://api.test"},
            )
            workers = WorkersConfig(
                pool=(worker,),
                timeout_seconds=5,
                tools=ToolConfig(network=False, web_search=False),
            )
            command = [
                sys.executable,
                "-c",
                "import sys,time; "
                "sys.stdout.write('first\\n'); sys.stdout.flush(); "
                "sys.stderr.write('diagnostic\\n'); sys.stderr.flush(); "
                "time.sleep(0.5); sys.stdout.write('second\\n'); sys.stdout.flush()",
            ]
            paths = prepare_trace(
                root=root / "trace",
                prompt="private prompt",
                command=[*command, "--api-key", "secret-value"],
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(backend="unsafe_local"),
                context={"join": {"attempt_id": "it001-i00-a00"}},
                secret_values=worker.env,
            )
            outcome: list[object] = []

            def run() -> None:
                outcome.append(
                    run_captured_process(
                        command=command,
                        prompt="",
                        paths=paths,
                        timeout_seconds=5,
                        environment={},
                        cwd=root,
                    )
                )

            thread = threading.Thread(target=run)
            thread.start()
            for _ in range(100):
                if paths.stdout.exists() and b"first" in paths.stdout.read_bytes():
                    break
                time.sleep(0.01)
            self.assertIn(b"first", paths.stdout.read_bytes())
            self.assertTrue(thread.is_alive(), "trace should be visible before the process exits")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(paths.stdout.read_bytes(), b"first\nsecond\n")
            self.assertEqual(paths.stderr.read_bytes(), b"diagnostic\n")
            self.assertEqual(stat.S_IMODE(paths.root.stat().st_mode), 0o700)
            for path in (
                paths.stdout,
                paths.stderr,
                paths.chunks,
                paths.input_dir / "prompt.txt",
                paths.input_dir / "resolved_worker.json",
                paths.input_dir / "argv.sanitized.json",
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            inputs = "".join(
                path.read_text(encoding="utf-8") for path in sorted(paths.input_dir.iterdir())
            )
            self.assertNotIn("secret-value", inputs)
            self.assertIn("<redacted>", inputs)
            argv = json.loads((paths.input_dir / "argv.sanitized.json").read_text())
            self.assertEqual(argv["argv"][-1], "<redacted>")
            transcript = root / "transcript.jsonl"
            render_transcript(paths, transcript)
            rendered = transcript.read_text(encoding="utf-8")
            self.assertIn("first", rendered)
            self.assertIn("diagnostic", rendered)
            self.assertIn("second", rendered)
            terminal = json.loads(paths.outcome.read_text(encoding="utf-8"))
            self.assertEqual(terminal["state"], "completed")
            self.assertTrue(terminal["complete"])
            self.assertEqual(terminal["streams"]["stdout"]["bytes"], len(b"first\nsecond\n"))
            self.assertEqual(len(terminal["streams"]["stdout"]["sha256"]), 64)

    def test_timeout_keeps_flushed_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = WorkerConfig(harness="claude", model="test-model")
            workers = WorkersConfig(
                pool=(worker,),
                timeout_seconds=1,
                tools=ToolConfig(network=False, web_search=False),
            )
            command = [
                sys.executable,
                "-c",
                "import sys,time; sys.stdout.write('before-timeout\\n'); "
                "sys.stdout.flush(); time.sleep(5)",
            ]
            paths = prepare_trace(
                root=root / "trace",
                prompt="",
                command=command,
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(backend="unsafe_local"),
            )
            result = run_captured_process(
                command=command,
                prompt="",
                paths=paths,
                timeout_seconds=1,
                environment={},
                cwd=root,
            )
            self.assertTrue(result.timed_out)
            self.assertEqual(result.returncode, 124)
            self.assertEqual(paths.stdout.read_bytes(), b"before-timeout\n")

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_timeout_terminates_descendants_that_inherit_trace_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = WorkerConfig(harness="codex", model="test-model")
            workers = WorkersConfig(pool=(worker,), timeout_seconds=1)
            command = ["/bin/sh", "-c", "printf inherited-pipe; sleep 30"]
            paths = prepare_trace(
                root=root / "trace",
                prompt="",
                command=command,
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(backend="unsafe_local"),
            )

            started = time.monotonic()
            result = run_captured_process(
                command=command,
                prompt="",
                paths=paths,
                timeout_seconds=1,
                environment={},
                cwd=root,
            )

            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(result.timed_out)
            self.assertEqual(paths.stdout.read_bytes(), b"inherited-pipe")

    def test_cancellation_event_terminates_process_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = WorkerConfig(harness="codex", model="test-model")
            workers = WorkersConfig(pool=(worker,), timeout_seconds=30)
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
            paths = prepare_trace(
                root=root / "trace",
                prompt="",
                command=command,
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(backend="unsafe_local"),
            )
            cancellation = threading.Event()
            threading.Timer(0.2, cancellation.set).start()

            result = run_captured_process(
                command=command,
                prompt="",
                paths=paths,
                timeout_seconds=30,
                environment={},
                cwd=root,
                cancellation_event=cancellation,
            )

            self.assertTrue(result.cancelled)
            self.assertEqual(result.returncode, 130)
            terminal = json.loads(paths.outcome.read_text(encoding="utf-8"))
            self.assertEqual(terminal["state"], "cancelled")
            self.assertEqual(terminal["termination_reason"], "controller_cancelled")

    def test_recovery_marks_missing_outcome_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            worker = WorkerConfig(harness="codex", model="test-model")
            paths = prepare_trace(
                root=run_dir / "traces" / "it001-i00-a00",
                prompt="private",
                command=["codex"],
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(backend="unsafe_local"),
            )

            self.assertEqual(recover_incomplete_traces(run_dir), (paths.root,))
            self.assertEqual(recover_incomplete_traces(run_dir), ())
            terminal = json.loads(paths.outcome.read_text(encoding="utf-8"))
            self.assertEqual(terminal["state"], "interrupted")
            self.assertTrue(terminal["truncated"])

    def test_recovery_covers_integrity_reviewer_role_traces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            worker = WorkerConfig(harness="claude", model="review-model")
            paths = prepare_trace(
                root=run_dir
                / "research"
                / "traces"
                / "integrity-reviewer"
                / "it001-i00-a00-r01",
                prompt="private review",
                command=["claude"],
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(backend="unsafe_local"),
            )

            self.assertEqual(recover_incomplete_traces(run_dir), (paths.root,))
            terminal = json.loads(paths.outcome.read_text(encoding="utf-8"))
            self.assertEqual(terminal["state"], "interrupted")
            self.assertTrue(terminal["cancelled"])


class TraceLedgerTests(unittest.TestCase):
    def test_index_joins_provenance_and_workspace_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            workspace = run_dir / "workspace"
            workspace.mkdir(parents=True)
            source = workspace / "solver.py"
            source.write_text("def solver(fun, x0):\n    return x0\n", encoding="utf-8")
            worker = WorkerConfig(harness="codex", model="test-model")
            workers = WorkersConfig(pool=(worker,), timeout_seconds=5)
            ledger = TraceLedger(
                run_dir,
                run_id="run-123",
                config_hash="c" * 64,
                adapter_name="cli",
            )
            paths = ledger.prepare(
                root=run_dir / "traces" / "it001-i00-a00",
                prompt="private prompt",
                command=[sys.executable, "-c", "print('ok')"],
                worker=worker,
                workers=workers,
                sandbox=SandboxConfig(backend="unsafe_local"),
                workspace=workspace,
                context={
                    "join": {
                        "module": "candidate_attempt",
                        "attempt_id": "it001-i00-a00",
                        "candidate_id": "it001-i00-a00",
                        "parent_id": "seed",
                        "iteration": 1,
                        "island": 0,
                    }
                },
            )
            invocation = json.loads(paths.invocation.read_text(encoding="utf-8"))
            input_hash = invocation["input_tree_hash"]
            self.assertEqual(invocation["schema"], "trace_invocation/2")
            self.assertEqual(invocation["run_id"], "run-123")
            self.assertEqual(invocation["adapter"], "cli")

            run_captured_process(
                command=[sys.executable, "-c", "print('ok')"],
                prompt="",
                paths=paths,
                timeout_seconds=5,
                environment={},
                cwd=workspace,
            )
            source.write_text(
                "def solver(fun, x0):\n    return x0\n# changed\n",
                encoding="utf-8",
            )
            first = ledger.record(paths, workspace=workspace)
            second = ledger.record(paths, workspace=workspace)
            self.assertEqual(first, second)
            self.assertNotEqual(first["input_tree_hash"], first["output_tree_hash"])
            self.assertEqual(first["input_tree_hash"], input_hash)
            self.assertEqual(first["capture_quality"], "complete")
            self.assertEqual(first["outcome"], "completed")
            self.assertEqual(first["join"]["parent_id"], "seed")

            entries = [
                json.loads(line)
                for line in ledger.index_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(entries, [first])
            self.assertEqual(stat.S_IMODE(ledger.index_path.stat().st_mode), 0o600)

            coverage = ledger.recover_and_summarize()
            self.assertEqual(coverage["total"], 1)
            self.assertEqual(coverage["capture_quality"]["complete"], 1)
            public = json.loads(ledger.public_coverage_path.read_text(encoding="utf-8"))
            self.assertEqual(public["capture_complete"], 1)
            serialized_public = json.dumps(public)
            self.assertNotIn("test-model", serialized_public)
            self.assertNotIn("trace_id", serialized_public)
            self.assertNotIn("relative_path", serialized_public)

    def test_recovery_indexes_interrupted_trace_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            workspace = run_dir / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "solver.py").write_text("def solver():\n    pass\n")
            worker = WorkerConfig(harness="claude", model="review-model")
            ledger = TraceLedger(
                run_dir,
                run_id="run-recovery",
                config_hash="d" * 64,
                adapter_name="cli",
            )
            (run_dir / "provenance.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-recovery",
                        "config_hash": "d" * 64,
                        "components": {"worker": {"adapter": "cli"}},
                    }
                ),
                encoding="utf-8",
            )
            paths = ledger.prepare(
                root=run_dir / "research" / "traces" / "custom-role" / "job-1",
                prompt="private",
                command=["claude"],
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(backend="unsafe_local"),
                workspace=workspace,
                context={"join": {"module": "custom", "role": "custom-role"}},
            )

            first = reconcile_trace_run(run_dir)
            second = reconcile_trace_run(run_dir)
            self.assertEqual(first, second)
            self.assertEqual(first["total"], 1)
            self.assertEqual(first["capture_quality"]["interrupted"], 1)
            self.assertEqual(first["outcomes"]["interrupted"], 1)
            self.assertEqual(
                len(ledger.index_path.read_text(encoding="utf-8").splitlines()),
                1,
            )
            outcome = json.loads(paths.outcome.read_text(encoding="utf-8"))
            self.assertFalse(outcome["complete"])
            self.assertTrue(outcome["truncated"])

    def test_concurrent_degraded_adapter_traces_are_indexed_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            worker = WorkerConfig(harness="codex", model="test-model")
            workers = WorkersConfig(pool=(worker,))
            ledger = TraceLedger(
                run_dir,
                run_id="run-concurrent",
                config_hash="e" * 64,
                adapter_name="injected-runner",
            )
            jobs: list[tuple[object, Path]] = []
            for index in range(12):
                workspace = run_dir / "workspaces" / f"job-{index}"
                workspace.mkdir(parents=True)
                (workspace / "solver.py").write_text(f"# {index}\n")
                paths = ledger.prepare(
                    root=run_dir / "traces" / f"job-{index}",
                    prompt="private",
                    command=["<worker-adapter>", "injected-runner"],
                    worker=worker,
                    workers=workers,
                    sandbox=SandboxConfig(backend="unsafe_local"),
                    workspace=workspace,
                    context={"join": {"attempt_id": f"job-{index}"}},
                )
                transcript = run_dir / "transcripts" / f"job-{index}.jsonl"
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text("fallback transcript\n", encoding="utf-8")
                finalize_adapter_trace(
                    paths,
                    transcript=transcript,
                    native_trace=None,
                    stderr_trace=None,
                    returncode=0,
                    timed_out=False,
                    cancelled=False,
                    capture_error=None,
                )
                jobs.append((paths, workspace))

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                futures = [
                    executor.submit(ledger.record, paths, workspace=root)
                    for paths, root in jobs
                ]
                for future in futures:
                    future.result(timeout=5)

            coverage = ledger.recover_and_summarize()
            self.assertEqual(coverage["total"], 12)
            self.assertEqual(coverage["capture_quality"]["degraded"], 12)
            entries = ledger.index_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(entries), 12)
            self.assertEqual(len({json.loads(line)["trace_id"] for line in entries}), 12)

    def test_torn_final_index_line_is_repaired_before_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            workspace = run_dir / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "solver.py").write_text("# seed\n")
            worker = WorkerConfig(harness="codex", model="test-model")
            ledger = TraceLedger(
                run_dir,
                run_id="run-torn",
                config_hash="f" * 64,
                adapter_name="injected-runner",
            )
            paths = ledger.prepare(
                root=run_dir / "traces" / "job",
                prompt="private",
                command=["adapter"],
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(backend="unsafe_local"),
                workspace=workspace,
            )
            transcript = run_dir / "transcript.txt"
            transcript.write_text("ok\n")
            finalize_adapter_trace(
                paths,
                transcript=transcript,
                native_trace=None,
                stderr_trace=None,
                returncode=0,
                timed_out=False,
                cancelled=False,
                capture_error=None,
            )
            ledger.record(paths, workspace=workspace)
            with ledger.index_path.open("ab") as handle:
                handle.write(b'{"trace_id": "torn')

            recovered = TraceLedger(
                run_dir,
                run_id="run-torn",
                config_hash="f" * 64,
                adapter_name="injected-runner",
            )
            coverage = recovered.recover_and_summarize()
            self.assertEqual(coverage["total"], 1)
            self.assertTrue(recovered.index_path.read_bytes().endswith(b"\n"))

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic-link test requires symlinks")
    def test_output_hash_never_follows_worker_created_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            workspace = run_dir / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "solver.py").write_text("# seed\n")
            outside = Path(directory) / "outside-secret.txt"
            outside.write_text("DO_NOT_READ_THIS_VALUE", encoding="utf-8")
            worker = WorkerConfig(harness="codex", model="test-model")
            ledger = TraceLedger(
                run_dir,
                run_id="run-symlink",
                config_hash="a" * 64,
                adapter_name="injected-runner",
            )
            paths = ledger.prepare(
                root=run_dir / "traces" / "job",
                prompt="private",
                command=["adapter"],
                worker=worker,
                workers=WorkersConfig(pool=(worker,)),
                sandbox=SandboxConfig(backend="unsafe_local"),
                workspace=workspace,
            )
            transcript = run_dir / "transcript.txt"
            transcript.write_text("ok\n")
            finalize_adapter_trace(
                paths,
                transcript=transcript,
                native_trace=None,
                stderr_trace=None,
                returncode=0,
                timed_out=False,
                cancelled=False,
                capture_error=None,
            )
            (workspace / "leak").symlink_to(outside)

            entry = ledger.record(paths, workspace=workspace)
            self.assertIsNone(entry["output_tree_hash"])
            self.assertEqual(entry["output_tree_state"], "unsafe_symlink")
            evidence = paths.workspace_evidence.read_text(encoding="utf-8")
            self.assertNotIn("DO_NOT_READ_THIS_VALUE", evidence)


if __name__ == "__main__":
    unittest.main()
