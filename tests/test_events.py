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
            self.assertEqual(rebuild_run_state(path)["run"]["status"], "running")

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
            phase = next(item for item in state["phases"] if item["name"] == "explore")
            attempt = next(
                item for item in state["attempts"] if item["attempt_id"] == "it001-i00-a00"
            )
            self.assertEqual(phase["status"], "running")
            self.assertEqual(attempt["status"], "pending")
            self.assertEqual(len(attempt["steps"]), 1)

    def test_public_run_state_has_stable_workflow_and_matrix_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public_events.jsonl"
            writer = EventWriter(path)
            attempt_scope = {
                "phase": "explore",
                "iteration": 1,
                "island": 0,
                "attempt_id": "it001-i00-a00",
            }
            writer.emit("run_started", "running")
            writer.emit("phase_started", "running", scope={"phase": "explore"})
            writer.emit(
                "iteration_started",
                "running",
                scope={"phase": "explore", "iteration": 1},
            )
            writer.emit(
                "attempt_started",
                "running",
                scope=attempt_scope,
                data={"parent_id": "seed", "guidance": "direction-1"},
            )
            writer.emit("worker_started", "running", scope=attempt_scope)
            writer.emit(
                "step_started",
                "running",
                scope={**attempt_scope, "step": "mutate", "step_idx": 0},
            )
            writer.emit(
                "step_finished",
                "succeeded",
                scope={**attempt_scope, "step": "mutate", "step_idx": 0},
                data={"verdict": "continue"},
            )
            writer.emit(
                "worker_finished",
                "succeeded",
                scope=attempt_scope,
                data={"returncode": 0, "trace_available": True},
            )
            writer.emit("integrity_review_started", "running", scope=attempt_scope)
            writer.emit(
                "integrity_review_finished",
                "succeeded",
                scope=attempt_scope,
                data={"gate": "approved"},
            )
            writer.emit(
                "attempt_finished",
                "succeeded",
                scope=attempt_scope,
                data={
                    "candidate_id": "it001-i00-a00",
                    "public_score": 0.75,
                    "valid": True,
                    "accepted": True,
                },
            )
            policy_scope = {
                "phase": "explore",
                "iteration": 1,
                "step": "migration",
                "step_idx": 0,
            }
            writer.emit("policy_started", "running", scope=policy_scope)
            writer.emit(
                "policy_finished",
                "succeeded",
                scope=policy_scope,
                data={"kill": [], "migrate": [], "stop": False},
            )
            writer.emit(
                "role_agent_started",
                "running",
                scope={
                    "phase": "strategy_analysis",
                    "island": 0,
                    "role": "analyst",
                    "job_id": "analysis-i0",
                },
            )
            writer.emit(
                "role_agent_finished",
                "succeeded",
                scope={
                    "phase": "strategy_analysis",
                    "island": 0,
                    "role": "analyst",
                    "job_id": "analysis-i0",
                },
                data={"returncode": 0, "trace_available": True},
            )
            writer.emit(
                "iteration_finished",
                "succeeded",
                scope={"phase": "explore", "iteration": 1},
                data={"attempt_count": 1, "stop": False},
            )
            writer.emit("phase_finished", "succeeded", scope={"phase": "explore"})
            writer.emit(
                "trace_coverage",
                "succeeded",
                data={"total": 2, "capture_complete": 2},
            )
            writer.emit(
                "run_finished",
                "succeeded",
                data={"best_candidate_id": "it001-i00-a00"},
            )
            writer.close()

            state = rebuild_run_state(path)

            self.assertEqual(state["schema"], "optiprofiler_evolve_public_run_state/1")
            self.assertEqual(state["run"]["status"], "succeeded")
            self.assertEqual(state["run"]["best_candidate_id"], "it001-i00-a00")
            self.assertEqual([item["name"] for item in state["phases"]], ["explore"])
            self.assertEqual(state["iterations"][0]["policies"][0]["name"], "migration")
            attempt = state["attempts"][0]
            self.assertEqual(attempt["status"], "succeeded")
            self.assertEqual(attempt["worker"]["status"], "succeeded")
            self.assertEqual(attempt["integrity_review"]["gate"], "approved")
            self.assertEqual(attempt["steps"][0]["verdict"], "continue")
            self.assertEqual(
                state["matrix"][0]["counts"],
                {
                    "cancelled": 0,
                    "failed": 0,
                    "pending": 0,
                    "running": 0,
                    "skipped": 0,
                    "succeeded": 1,
                    "accepted": 1,
                    "quarantined": 0,
                },
            )
            self.assertEqual(state["roles"][0]["job_id"], "analysis-i0")
            self.assertEqual(state["trace_coverage"]["capture_complete"], 2)

    def test_public_run_state_drops_private_trace_coverage_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public_events.jsonl"
            writer = EventWriter(path)
            writer.emit(
                "trace_coverage",
                "succeeded",
                data={
                    "total": 2,
                    "capture_complete": 1,
                    "capture_degraded": None,
                    "trace_ids": ["TRACE_SECRET_771"],
                    "upstream": "UPSTREAM_SECRET_771",
                    "private_index": "/private/TRACE_INDEX_SECRET_771",
                },
            )
            writer.close()

            coverage = rebuild_run_state(path)["trace_coverage"]

            self.assertEqual(
                coverage,
                {
                    "status": "succeeded",
                    "total": 2,
                    "capture_complete": 1,
                    "capture_degraded": None,
                },
            )

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
                    "guidance": {"nested": "SECRET_NESTED"},
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
            writer.emit(
                "provider_gateway_finished",
                "succeeded",
                scope={"attempt_id": "it001-i00-a00"},
                data={
                    "outcome": "completed",
                    "request_count": 3,
                    "upstream": "SECRET_PROVIDER_HOST",
                    "credential": "SECRET_PROVIDER_KEY",
                    "audit_path": "/private/SECRET_GATEWAY_TRACE",
                },
            )
            writer.close()

            projected = project_public_events(source, destination)

            self.assertEqual(len(projected), 2)
            serialized = destination.read_text(encoding="utf-8")
            self.assertIn("public_score", serialized)
            for secret in (
                "validation_score",
                "SECRET_TRACE",
                "SECRET_ERROR",
                "SECRET_SCOPE",
                "SECRET_FUTURE",
                "SECRET_NESTED",
                "SECRET_PROVIDER_HOST",
                "SECRET_PROVIDER_KEY",
                "SECRET_GATEWAY_TRACE",
            ):
                self.assertNotIn(secret, serialized)
            event = json.loads(serialized.splitlines()[0])
            self.assertEqual(event["seq"], 1)
            self.assertEqual(event["scope"]["attempt_id"], "it001-i00-a00")
            gateway = json.loads(serialized.splitlines()[1])
            self.assertEqual(gateway["data"], {"outcome": "completed", "request_count": 3})


if __name__ == "__main__":
    unittest.main()
