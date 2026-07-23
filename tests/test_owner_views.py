from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from optiprofiler_evolve.owner_views import render_owner_views
from optiprofiler_evolve.projections import project_public_events
from optiprofiler_evolve.viewers import (
    materialize_public_bundle,
    render_final_report,
    render_public_report,
    render_status,
)

MODEL_CANARY = "MODEL_SECRET_881"
VALIDATION_CANARY = "0.428142"
FINDING_CANARY = "REVIEWER_FINDING_SECRET_881"
TRANSCRIPT_CANARY = "RAW_TRACE_SECRET_881"

ATTEMPT = "it001-i00-a00"
REVIEW_JOB = f"{ATTEMPT}-r01"
ROLE_JOB = "job-dir-1"

T = "2026-07-23T10:{minute:02d}:{second:02d}+00:00"


def _ts(minute: int, second: int = 0) -> str:
    return T.format(minute=minute, second=second)


def _write_events(path: Path, rows: list[tuple[str, str, str, dict, dict]]) -> None:
    lines = []
    for seq, (ts, kind, status, scope, data) in enumerate(rows, start=1):
        lines.append(
            json.dumps(
                {"seq": seq, "ts": ts, "kind": kind, "scope": scope, "status": status, "data": data},
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _attempt_rows(run_dir: Path, *, terminal: bool = True) -> list:
    scope = {"phase": "explore", "iteration": 1, "island": 0, "attempt_id": ATTEMPT}
    review_scope = {**scope}
    rows = [
        (_ts(0), "run_started", "running", {}, {}),
        (_ts(0, 1), "phase_started", "running", {"phase": "explore"}, {}),
        (_ts(0, 2), "iteration_started", "running", {"iteration": 1}, {}),
        (
            _ts(0, 5),
            "attempt_started",
            "running",
            scope,
            {"parent_id": "seed", "worker": f"claude:{MODEL_CANARY}", "guidance": "card-7"},
        ),
        (_ts(0, 6), "worker_started", "running", scope, {"worker": f"claude:{MODEL_CANARY}"}),
        (
            _ts(2, 6),
            "worker_finished",
            "succeeded",
            scope,
            {
                "returncode": 0,
                "timed_out": False,
                "cancelled": False,
                "termination_reason": None,
                "transcript": str(run_dir / "transcripts" / f"{ATTEMPT}.jsonl"),
                "native_trace": None,
                "stderr_trace": str(run_dir / "traces" / ATTEMPT / "raw.stderr.stream"),
                "trace_chunks": str(run_dir / "traces" / ATTEMPT / "chunks.jsonl"),
                "trace_outcome": str(run_dir / "traces" / ATTEMPT / "outcome.json"),
                "trace_capture_error": None,
                "provider_gateway_outcome": "completed",
                "provider_gateway_request_count": 7,
                "provider_gateway_manifest": None,
            },
        ),
        (
            _ts(2, 10),
            "step_finished",
            "succeeded",
            {**scope, "step": "public_evaluate", "step_idx": 3},
            {
                "verdict": "pass",
                "metrics": {"public_score": 0.75, "public_success": True},
                "artifacts": [str(run_dir / "controller" / "brokers" / ATTEMPT / "artifacts")],
                "error": None,
            },
        ),
        (_ts(2, 20), "integrity_review_started", "running", review_scope, {}),
        (
            _ts(2, 21),
            "role_agent_started",
            "running",
            {"role": "integrity-reviewer", "job_id": REVIEW_JOB, "phase": "explore"},
            {},
        ),
        (
            _ts(3, 1),
            "role_agent_finished",
            "succeeded",
            {"role": "integrity-reviewer", "job_id": REVIEW_JOB, "phase": "explore"},
            {"returncode": 0, "timed_out": False, "outputs": ["review.json"]},
        ),
        (
            _ts(3, 2),
            "integrity_review_attempt_finished",
            "succeeded",
            review_scope,
            {
                "review_attempt": 1,
                "verdict": "approve",
                "finding_count": 1,
                "report": str(
                    run_dir / "controller" / "integrity_reviews" / ATTEMPT / "attempt_01.json"
                ),
            },
        ),
        (
            _ts(3, 3),
            "integrity_review_finished",
            "succeeded",
            review_scope,
            {"gate": "approved"},
        ),
    ]
    if terminal:
        rows.extend(
            [
                (
                    _ts(3, 30),
                    "attempt_finished",
                    "succeeded",
                    scope,
                    {
                        "candidate_id": ATTEMPT,
                        "parent_id": "seed",
                        "guidance": "card-7",
                        "public_score": 0.75,
                        "validation_score": float(VALIDATION_CANARY),
                        "review_verdict": "approve",
                        "valid": True,
                        "accepted": True,
                        "error": None,
                        "changed_files": ["solver.py"],
                        "worker_returncode": 0,
                        "worker_timed_out": False,
                    },
                ),
                (
                    _ts(4),
                    "role_agent_started",
                    "running",
                    {"role": "director", "job_id": ROLE_JOB, "phase": "explore"},
                    {},
                ),
                (
                    _ts(5),
                    "role_agent_finished",
                    "succeeded",
                    {"role": "director", "job_id": ROLE_JOB, "phase": "explore"},
                    {"returncode": 0, "timed_out": False, "outputs": []},
                ),
                (_ts(5, 30), "iteration_finished", "succeeded", {"iteration": 1}, {}),
                (_ts(5, 40), "phase_finished", "succeeded", {"phase": "explore"}, {}),
                (
                    _ts(6),
                    "run_finished",
                    "succeeded",
                    {},
                    {"best_candidate_id": ATTEMPT},
                ),
            ]
        )
    return rows


def _build_run_dir(root: Path, *, terminal: bool = True) -> Path:
    run_dir = root / "run"
    run_dir.mkdir()
    _write_events(run_dir / "events.jsonl", _attempt_rows(run_dir, terminal=terminal))

    transcript = run_dir / "transcripts" / f"{ATTEMPT}.jsonl"
    transcript.parent.mkdir(parents=True)
    entries = [
        json.dumps({"type": "message", "role": "assistant", "text": TRANSCRIPT_CANARY}),
        json.dumps({"type": "tool_use", "name": "edit", "input": {"text": "<script>alert(1)</script>"}}),
    ]
    entries.extend(
        json.dumps({"type": "message", "text": f"padding line {index} " + "x" * 600})
        for index in range(400)
    )
    transcript.write_text("\n".join(entries) + "\n", encoding="utf-8")

    traces = run_dir / "traces" / ATTEMPT
    traces.mkdir(parents=True)
    (traces / "raw.stdout.stream").write_text("worker stdout\n", encoding="utf-8")
    (traces / "raw.stderr.stream").write_text("worker stderr\n", encoding="utf-8")
    (traces / "chunks.jsonl").write_text("{}\n", encoding="utf-8")
    (traces / "outcome.json").write_text("{}\n", encoding="utf-8")

    (run_dir / "candidates" / "seed").mkdir(parents=True)
    (run_dir / "candidates" / "seed" / "solver.py").write_text(
        "def solver(fun, x0):\n    return x0\n", encoding="utf-8"
    )
    (run_dir / "workspaces" / ATTEMPT).mkdir(parents=True)
    (run_dir / "workspaces" / ATTEMPT / "solver.py").write_text(
        "def solver(fun, x0):\n    return improved(x0)\n", encoding="utf-8"
    )

    reviews = run_dir / "controller" / "integrity_reviews" / ATTEMPT
    reviews.mkdir(parents=True)
    decision = {
        "verdict": "approve",
        "summary": FINDING_CANARY,
        "reason": None,
        "findings": [
            {"severity": "note", "summary": FINDING_CANARY, "evidence": "candidate/solver.py"}
        ],
    }
    (reviews / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    (reviews / "attempt_01.json").write_text(json.dumps(decision), encoding="utf-8")

    validation = run_dir / "controller" / "evaluations" / ATTEMPT / "validation"
    validation.mkdir(parents=True)
    (validation / "result.json").write_text(
        json.dumps({"score": float(VALIDATION_CANARY)}), encoding="utf-8"
    )

    broker = run_dir / "controller" / "brokers" / ATTEMPT / "artifacts"
    broker.mkdir(parents=True)
    (broker / "feedback.md").write_text("# Evaluation\n", encoding="utf-8")

    research = run_dir / "research"
    (research / "transcripts" / "integrity-reviewer").mkdir(parents=True)
    (research / "transcripts" / "integrity-reviewer" / f"{REVIEW_JOB}.jsonl").write_text(
        json.dumps({"type": "message", "text": "reviewer transcript"}) + "\n",
        encoding="utf-8",
    )
    (research / "traces" / "integrity-reviewer" / REVIEW_JOB).mkdir(parents=True)
    (research / "traces" / "integrity-reviewer" / REVIEW_JOB / "raw.stdout.stream").write_text(
        "reviewer stdout\n", encoding="utf-8"
    )
    (research / "transcripts" / "director").mkdir(parents=True)
    (research / "transcripts" / "director" / f"{ROLE_JOB}.jsonl").write_text(
        json.dumps({"type": "message", "text": "director transcript"}) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _hrefs(page: Path) -> list[str]:
    return re.findall(r'href="([^"]+)"', page.read_text(encoding="utf-8"))


class OwnerViewTests(unittest.TestCase):
    def test_owner_pages_show_private_evidence_and_all_links_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _build_run_dir(Path(directory))
            render_owner_views(run_dir / "events.jsonl", run_dir, final=True)

            index = run_dir / "status.html"
            attempt_page = run_dir / "owner" / "attempts" / f"{ATTEMPT}.html"
            review_page = run_dir / "owner" / "roles" / f"{REVIEW_JOB}.html"
            role_page = run_dir / "owner" / "roles" / f"{ROLE_JOB}.html"
            for page in (index, attempt_page, review_page, role_page):
                self.assertTrue(page.is_file(), page)

            index_text = index.read_text(encoding="utf-8")
            self.assertIn("PRIVATE", index_text)
            self.assertIn("public/", index_text)
            self.assertIn(f"owner/attempts/{ATTEMPT}.html", index_text)
            self.assertIn(f"owner/roles/{REVIEW_JOB}.html", index_text)
            self.assertIn(f"owner/roles/{ROLE_JOB}.html", index_text)
            self.assertIn(VALIDATION_CANARY[:6], index_text)

            attempt_text = attempt_page.read_text(encoding="utf-8")
            self.assertIn("PRIVATE", attempt_text)
            self.assertIn(MODEL_CANARY, attempt_text)
            self.assertIn(VALIDATION_CANARY, attempt_text)
            self.assertIn(FINDING_CANARY, attempt_text)
            self.assertIn(TRANSCRIPT_CANARY, attempt_text)
            self.assertIn("never worker-visible", attempt_text)
            self.assertIn("unavailable (finalists only)", attempt_text)
            self.assertIn("solver.py", attempt_text)
            self.assertIn("diff.patch", attempt_text)
            self.assertIn(f"../roles/{REVIEW_JOB}.html", attempt_text)
            self.assertIn("completed", attempt_text)

            # Hostile transcript content is escaped, and previews are bounded.
            self.assertNotIn("<script", attempt_text.lower())
            self.assertIn("Preview truncated", attempt_text)
            self.assertLess(attempt_page.stat().st_size, 400_000)

            # Every relative link on every owner page resolves on disk.
            for page in (index, attempt_page, review_page, role_page):
                for href in _hrefs(page):
                    if href.startswith("#"):
                        continue
                    target = (page.parent / unquote(href)).resolve()
                    self.assertTrue(target.exists(), f"{page.name} -> {href}")

            # Terminal run: no refresh anywhere.
            self.assertNotIn('http-equiv="refresh"', index_text)
            self.assertNotIn('http-equiv="refresh"', attempt_text)

    def test_running_attempt_pages_refresh_and_report_unavailable_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _build_run_dir(Path(directory), terminal=False)
            render_owner_views(run_dir / "events.jsonl", run_dir)

            index_text = (run_dir / "status.html").read_text(encoding="utf-8")
            attempt_text = (
                run_dir / "owner" / "attempts" / f"{ATTEMPT}.html"
            ).read_text(encoding="utf-8")
            self.assertIn('http-equiv="refresh"', index_text)
            self.assertIn('http-equiv="refresh"', attempt_text)
            self.assertIn("unavailable (not evaluated)", attempt_text)

    def test_public_bundle_never_contains_owner_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _build_run_dir(Path(directory))
            render_owner_views(run_dir / "events.jsonl", run_dir, final=True)

            public_events = run_dir / "public_events.jsonl"
            project_public_events(run_dir / "events.jsonl", public_events)
            state = render_status(public_events, run_dir / "public_status.html")
            render_public_report(state, run_dir / "PUBLIC_REPORT.md")
            render_final_report(public_events, run_dir / "report.html")
            (run_dir / "public_trace_coverage.json").write_text("{}", encoding="utf-8")

            materialize_public_bundle(run_dir)

            public = run_dir / "public"
            names = {path.name for path in public.iterdir()}
            self.assertEqual(
                names,
                {
                    "public_events.jsonl",
                    "public_run_state.json",
                    "status.html",
                    "report.html",
                    "public_trace_coverage.json",
                    "PUBLIC_REPORT.md",
                },
            )
            bundle = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in sorted(public.iterdir())
            )
            for canary in (
                MODEL_CANARY,
                VALIDATION_CANARY,
                FINDING_CANARY,
                TRANSCRIPT_CANARY,
            ):
                self.assertNotIn(canary, bundle)
            self.assertNotIn("owner/", bundle)
            self.assertNotIn("MANIFEST", bundle)

    def test_manifest_is_relative_derived_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _build_run_dir(Path(directory))
            render_owner_views(run_dir / "events.jsonl", run_dir, final=True)

            manifest = json.loads(
                (run_dir / "owner" / "MANIFEST.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema"], "optiprofiler_evolve_owner_manifest/1")
            attempt_entries = {
                entry["attempt_id"]: entry for entry in manifest["attempts"]
            }
            self.assertIn(ATTEMPT, attempt_entries)
            evidence = attempt_entries[ATTEMPT]["evidence"]
            self.assertIn("transcript", evidence)
            self.assertIn("integrity_reviews", evidence)
            text = json.dumps(manifest)
            for item in re.findall(r'"path": "([^"]+)"', text):
                self.assertFalse(Path(item).is_absolute(), item)
                self.assertNotIn("..", item)
                self.assertFalse(item.startswith("public"), item)
            self.assertNotIn(MODEL_CANARY, text)
            self.assertNotIn(TRANSCRIPT_CANARY, text)


if __name__ == "__main__":
    unittest.main()
