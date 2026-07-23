from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.events import EventWriter
from optiprofiler_evolve.projections import project_public_events
from optiprofiler_evolve.viewers import (
    PUBLIC_BUNDLE_FILES,
    materialize_public_bundle,
    render_final_report,
    render_public_report,
    render_status,
)


class PublicViewerTests(unittest.TestCase):
    def test_status_escapes_public_values_and_uses_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "public_events.jsonl"
            writer = EventWriter(events)
            writer.emit("run_started", "running")
            writer.emit("phase_started", "running", scope={"phase": "explore"})
            writer.emit(
                "attempt_started",
                "running",
                scope={
                    "phase": "explore",
                    "iteration": 1,
                    "island": 0,
                    "attempt_id": '<script data-secret="x">alert(1)</script>',
                },
            )
            writer.close()

            state = render_status(events, root / "status.html")
            render_public_report(state, root / "PUBLIC_REPORT.md")
            status = (root / "status.html").read_text(encoding="utf-8")

            self.assertIn('http-equiv="refresh"', status)
            self.assertIn("&lt;script data-secret=&quot;x&quot;&gt;", status)
            self.assertNotIn("<script", status.lower())
            self.assertNotIn("fetch(", status)
            self.assertNotIn("http://", status)
            self.assertNotIn("https://", status)
            self.assertNotIn("src=", status.lower())
            self.assertEqual(
                state["schema"],
                "optiprofiler_evolve_public_run_state/1",
            )

    def test_public_bundle_is_an_exact_allowlist_without_private_canaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_events = root / "events.jsonl"
            public_events = root / "public_events.jsonl"
            writer = EventWriter(private_events)
            writer.emit(
                "run_started",
                "running",
                data={
                    "model": "MODEL_SECRET_771",
                    "provider": "PROVIDER_SECRET_771",
                },
            )
            writer.emit("phase_started", "running", scope={"phase": "explore"})
            writer.emit(
                "attempt_finished",
                "succeeded",
                scope={
                    "phase": "explore",
                    "iteration": 1,
                    "island": 0,
                    "attempt_id": "candidate-1",
                },
                data={
                    "candidate_id": "candidate-1",
                    "public_score": 0.75,
                    "validation_score": "VALIDATION_SECRET_771",
                    "hidden_score": "HIDDEN_SECRET_771",
                    "transcript": "RAW_TRACE_SECRET_771",
                    "error": "REVIEWER_FINDING_SECRET_771",
                },
            )
            writer.emit(
                "provider_gateway_finished",
                "succeeded",
                scope={"attempt_id": "candidate-1"},
                data={
                    "outcome": "completed",
                    "request_count": 2,
                    "inflight_request_count": "INFLIGHT_SECRET_771",
                    "upstream": "UPSTREAM_SECRET_771",
                    "trace_ids": "TRACE_ID_SECRET_771",
                },
            )
            writer.emit(
                "trace_coverage",
                "succeeded",
                data={"total": 1, "capture_complete": 1},
            )
            writer.emit(
                "run_finished",
                "succeeded",
                data={"best_candidate_id": "candidate-1"},
            )
            writer.close()

            project_public_events(private_events, public_events)
            state = render_status(public_events, root / "status.html")
            render_public_report(state, root / "PUBLIC_REPORT.md")
            render_final_report(public_events, root / "report.html")
            (root / "public_trace_coverage.json").write_text(
                json.dumps({"total": 1, "capture_complete": 1}),
                encoding="utf-8",
            )
            (root / "FINAL_REPORT.md").write_text(
                "VALIDATION_SECRET_771 HIDDEN_SECRET_771",
                encoding="utf-8",
            )
            (root / "controller").mkdir()
            (root / "controller" / "review.json").write_text(
                "REVIEWER_FINDING_SECRET_771",
                encoding="utf-8",
            )
            (root / "traces").mkdir()
            (root / "traces" / "stdout.raw").write_text(
                "RAW_TRACE_SECRET_771",
                encoding="utf-8",
            )
            (root / "public").mkdir()
            (root / "public" / "stale-private.txt").write_text(
                "PRIVATE_PATH_SECRET_771",
                encoding="utf-8",
            )

            copied = materialize_public_bundle(root)

            expected = set(PUBLIC_BUNDLE_FILES)
            actual = {path.name for path in (root / "public").iterdir()}
            self.assertEqual(actual, expected)
            self.assertEqual({path.name for path in copied}, expected)
            bundle = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in sorted((root / "public").iterdir())
            )
            for canary in (
                "MODEL_SECRET_771",
                "PROVIDER_SECRET_771",
                "VALIDATION_SECRET_771",
                "HIDDEN_SECRET_771",
                "RAW_TRACE_SECRET_771",
                "REVIEWER_FINDING_SECRET_771",
                "INFLIGHT_SECRET_771",
                "UPSTREAM_SECRET_771",
                "TRACE_ID_SECRET_771",
                "PRIVATE_PATH_SECRET_771",
            ):
                self.assertNotIn(canary, bundle)
            self.assertNotIn("FINAL_REPORT.md", bundle)
            self.assertNotIn("controller/", bundle)
            self.assertNotIn("traces/", bundle)

    def test_terminal_status_stops_refreshing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "public_events.jsonl"
            writer = EventWriter(events)
            writer.emit("run_started", "running")
            writer.emit("run_finished", "failed")
            writer.close()

            render_status(events, root / "status.html")

            status = (root / "status.html").read_text(encoding="utf-8")
            self.assertNotIn('http-equiv="refresh"', status)
            self.assertIn("failed", status)


if __name__ == "__main__":
    unittest.main()
