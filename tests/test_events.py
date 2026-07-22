from __future__ import annotations

import tempfile
import threading
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.events import EventWriter, read_events, rebuild_run_state
from optiprofiler_evolve.projections import project_public_events


class EventLedgerTests(unittest.TestCase):
    def test_parallel_emit_has_one_contiguous_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            writer = EventWriter(path)

            def emit(island: int) -> None:
                for iteration in range(20):
                    writer.emit(
                        "step_finished",
                        "succeeded",
                        scope={"iteration": iteration, "island": island},
                    )

            threads = [threading.Thread(target=emit, args=(island,)) for island in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            writer.close()
            events = read_events(path)
            self.assertEqual(len(events), 80)
            self.assertEqual([event["seq"] for event in events], list(range(1, 81)))

    def test_reader_ignores_only_a_torn_final_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                '{"seq":1,"kind":"run_started","scope":{},"status":"running","data":{}}\n{"seq":2',
                encoding="utf-8",
            )
            events = read_events(path)
            self.assertEqual(len(events), 1)
            self.assertEqual(rebuild_run_state(path)["run"], "running")

    def test_writer_failure_is_reported_without_queue_deadlock(self) -> None:
        class BrokenFile:
            def __enter__(self) -> "BrokenFile":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def write(self, _value: str) -> None:
                raise OSError("disk full")

            def flush(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with patch("pathlib.Path.open", return_value=BrokenFile()):
                writer = EventWriter(path)
                writer.emit("run_started", "running")
                with self.assertRaisesRegex(RuntimeError, "Event writer failed"):
                    writer.flush()
                with self.assertRaisesRegex(RuntimeError, "Event writer failed"):
                    writer.close()

    def test_writer_keeps_draining_after_concurrent_failure(self) -> None:
        write_started = threading.Event()
        release_failure = threading.Event()

        class BlockingBrokenFile:
            def __enter__(self) -> "BlockingBrokenFile":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def write(self, _value: str) -> None:
                write_started.set()
                release_failure.wait(timeout=2)
                raise OSError("disk full")

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with patch("pathlib.Path.open", return_value=BlockingBrokenFile()):
                writer = EventWriter(path)
                writer.emit("run_started", "running")
                self.assertTrue(write_started.wait(timeout=2))
                for index in range(20):
                    writer.emit("step_started", "running", data={"index": index})
                release_failure.set()
                with self.assertRaisesRegex(RuntimeError, "Event writer failed"):
                    writer.flush()
                self.assertTrue(writer._thread.is_alive())
                with self.assertRaisesRegex(RuntimeError, "Event writer failed"):
                    writer.emit("run_finished", "failed")
                with self.assertRaisesRegex(RuntimeError, "Event writer failed"):
                    writer.close()

    def test_step_failure_does_not_overwrite_phase_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            writer = EventWriter(path)
            writer.emit("phase_started", "running", scope={"phase": "explore"})
            writer.emit(
                "step_finished",
                "failed",
                scope={
                    "phase": "explore",
                    "iteration": 1,
                    "island": 0,
                    "attempt_id": "it001-i00-a00",
                    "step": "smoke",
                    "step_idx": 2,
                },
            )
            writer.close()
            state = rebuild_run_state(path)
            self.assertEqual(state["phases"]["explore"], "running")
            self.assertEqual(len(state["attempts"]["it001-i00-a00"]["steps"]), 1)

    def test_public_projection_is_default_deny_by_kind_and_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "events.jsonl"
            destination = root / "public_events.jsonl"
            writer = EventWriter(source)
            writer.emit(
                "attempt_finished",
                "succeeded",
                scope={
                    "phase": "explore",
                    "attempt_id": "it001-i00-a00",
                    "private_scope": "SECRET_SCOPE",
                },
                data={
                    "candidate_id": "it001-i00-a00",
                    "public_score": 0.75,
                    "validation_score": 0.99,
                    "transcript": "/private/SECRET_TRACE",
                    "error": "SECRET_ERROR",
                },
            )
            writer.emit(
                "future_private_event",
                "succeeded",
                data={"payload": "SECRET_FUTURE"},
            )
            writer.close()

            projected = project_public_events(source, destination)

            self.assertEqual(len(projected), 1)
            serialized = destination.read_text(encoding="utf-8")
            self.assertIn("public_score", serialized)
            for secret in (
                "validation_score",
                "SECRET_TRACE",
                "SECRET_ERROR",
                "SECRET_SCOPE",
                "SECRET_FUTURE",
            ):
                self.assertNotIn(secret, serialized)
            event = json.loads(serialized)
            self.assertEqual(event["seq"], 1)
            self.assertEqual(event["scope"]["attempt_id"], "it001-i00-a00")


if __name__ == "__main__":
    unittest.main()
