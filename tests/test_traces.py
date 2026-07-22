from __future__ import annotations

import json
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from optiprofiler_evolve.config import (
    SandboxConfig,
    ToolConfig,
    WorkerConfig,
    WorkersConfig,
)
from optiprofiler_evolve.traces import (
    prepare_trace,
    render_transcript,
    run_captured_process,
)


class TraceCaptureTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
