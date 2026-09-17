from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from optiprofiler_evolve.agents.explanations import capture_solver_explanation
from optiprofiler_evolve.models import FinalistResult
from optiprofiler_evolve.phases.report import ReportContext, ReportPhase
from optiprofiler_evolve.solver import tree_hash

_SEED = "def solver(fun, x0):\n    return x0\n"
_STALE = "STALE: returns the starting point without any objective call."
_FRESH = "FRESH: evaluates one shifted trial point and keeps the better point."


def _explanation(summary: str) -> str:
    return json.dumps({
        "schema": "solver_explanation/1", "summary": summary, "pseudocode": ["step"],
        "mechanisms": [{"name": "m", "description": "d",
                        "code_bindings": [{"file": "solver.py", "lines": [1, 2]}]}],
    })


def _worker_session(root: Path, *, edit_after_explaining: bool) -> Path:
    """A worker explains its solver, then (optionally) keeps editing the code."""
    workspace = root / "workspace"
    metadata = workspace / ".optiprofiler_evolve"
    metadata.mkdir(parents=True)
    solver = workspace / "solver.py"
    solver.write_text(_SEED)
    os.utime(solver, ns=(1_000, 1_000))
    explanation = metadata / "solver_explanation.json"
    explanation.write_text(_explanation(_STALE))
    os.utime(explanation, ns=(2_000, 2_000))
    if edit_after_explaining:
        solver.write_text(
            "def solver(fun, x0):\n    trial = x0 + 0.1\n"
            "    return trial if fun(trial) < fun(x0) else x0\n"
        )
        os.utime(solver, ns=(3_000, 3_000))
    return workspace


def _run_report(root: Path, candidate_id: str, final_solver: Path, *, required: bool):
    (root / "candidates/seed").mkdir(parents=True, exist_ok=True)
    (root / "candidates/seed/solver.py").write_text(_SEED)
    jobs = []

    def explainer(job):
        jobs.append(job)
        workspace = root / f"author-{len(jobs)}"
        shutil.copytree(job.inputs["candidate"], workspace / "candidate")
        metadata = workspace / "candidate/.optiprofiler_evolve"
        metadata.mkdir()
        (metadata / "solver_explanation.json").write_text(_explanation(_FRESH))
        return SimpleNamespace(workspace=workspace, success=True)

    result = ReportPhase(require_explanation=required).run(ReportContext(
        run_dir=root, options={}, artifacts={}, emit=lambda *a, **k: None,
        services=SimpleNamespace(run_trusted_agent=explainer),
        final_result=FinalistResult(candidate_id, 0, 0.5, 0.5, 0.5, root),
        final_solver=final_solver,
    ))
    return result, jobs


class ExplanationCodeIdentityTests(unittest.TestCase):
    def test_capture_records_snapshot_and_detects_a_later_code_edit(self) -> None:
        for edited in (False, True):
            with self.subTest(edited=edited), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = _worker_session(root, edit_after_explaining=edited)
                captured = json.loads(capture_solver_explanation(
                    workspace, root / "explanations/a.json", attempt_id="a").read_text())
                self.assertEqual(captured["status"], "complete")
                self.assertEqual(captured["code_identity"], {
                    "origin": "worker",
                    "captured_tree_hash": tree_hash(workspace),
                    "code_edited_after_explanation": edited,
                })

    def test_matching_hash_does_not_launder_prose_written_before_the_last_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _worker_session(root, edit_after_explaining=True)
            capture_solver_explanation(workspace, root / "explanations/a.json", attempt_id="a")
            final_solver = root / "final_solver"
            shutil.copytree(workspace, final_solver)
            # The captured hash equals the delivered hash, yet the prose predates the code.
            self.assertEqual(
                json.loads((root / "explanations/a.json").read_text())["code_identity"]
                ["captured_tree_hash"], tree_hash(final_solver))

            result, jobs = _run_report(root, "a", final_solver, required=True)

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(jobs), 1)
            final = (root / "FINAL_SOLVER_EXPLANATION.md").read_text()
            self.assertIn(_FRESH, final)
            self.assertNotIn(_STALE, final)
            self.assertIn("final-explainer-authored", final)
            self.assertIn("not machine-verified", final)
            self.assertIn(_STALE, (root / "explanations/a.superseded.json").read_text())
            delivery = json.loads((root / "final_delivery.json").read_text())
            self.assertTrue(delivery["explanation_provenance"]["valid"])
            self.assertEqual(delivery["solver_tree_hash"], tree_hash(final_solver))

    def test_optional_report_shows_stale_status_instead_of_stale_prose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _worker_session(root, edit_after_explaining=True)
            capture_solver_explanation(workspace, root / "explanations/a.json", attempt_id="a")
            final_solver = root / "final_solver"
            shutil.copytree(workspace, final_solver)

            result, jobs = _run_report(root, "a", final_solver, required=False)

            self.assertEqual((result.status, jobs), ("succeeded", []))
            final = (root / "FINAL_SOLVER_EXPLANATION.md").read_text()
            self.assertNotIn(_STALE, final)
            self.assertIn("edited after the explanation was written", final)

    def test_prose_for_another_snapshot_and_legacy_records_are_not_reused(self) -> None:
        for case in ("other_snapshot", "legacy"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = _worker_session(root, edit_after_explaining=False)
                destination = root / "explanations/a.json"
                capture_solver_explanation(workspace, destination, attempt_id="a")
                final_solver = root / "final_solver"
                shutil.copytree(workspace, final_solver)
                if case == "other_snapshot":
                    (final_solver / "solver.py").write_text(_SEED + "# later variant\n")
                else:
                    payload = json.loads(destination.read_text())
                    del payload["code_identity"]
                    destination.write_text(json.dumps(payload))

                result, jobs = _run_report(root, "a", final_solver, required=True)

                self.assertEqual((result.status, len(jobs)), ("succeeded", 1))
                self.assertNotIn(_STALE, (root / "FINAL_SOLVER_EXPLANATION.md").read_text())

    def test_untouched_worker_explanation_is_reused_without_a_repair_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _worker_session(root, edit_after_explaining=False)
            capture_solver_explanation(workspace, root / "explanations/a.json", attempt_id="a")
            final_solver = root / "final_solver"
            shutil.copytree(workspace, final_solver)

            result, jobs = _run_report(root, "a", final_solver, required=True)

            self.assertEqual((result.status, jobs), ("succeeded", []))
            final = (root / "FINAL_SOLVER_EXPLANATION.md").read_text()
            self.assertIn(_STALE, final)  # here the "stale" text is simply the worker's prose
            self.assertIn("worker-authored", final)


class StagedReviewProvenanceTests(unittest.TestCase):
    def _assessments(self, root: Path, reviewed_hash: str, integrity: dict) -> None:
        criteria = {name: {"weight": 1.0} for name in ("integrity", "performance")}
        skipped = {"status": "skipped", "score": None, "error": "disabled"}
        for relative, results, profile in (
            ("evaluations/a/validation", {"integrity": integrity, "performance": {
                "status": "succeeded", "score": 0.7, "error": None}}, "validation"),
            ("final_evaluations/a", {"integrity": skipped, "performance": {
                "status": "succeeded", "score": 0.3, "error": None}}, "hidden"),
        ):
            path = root / "controller" / relative
            path.mkdir(parents=True)
            (path / "assessment.json").write_text(json.dumps({
                "profile": profile, "tree_hash": reviewed_hash, "criteria": criteria,
                "results": results}))

    def test_report_separates_validation_review_from_hidden_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _worker_session(root, edit_after_explaining=False)
            capture_solver_explanation(workspace, root / "explanations/a.json", attempt_id="a")
            final_solver = root / "final_solver"
            shutil.copytree(workspace, final_solver)
            self._assessments(root, tree_hash(final_solver),
                              {"status": "succeeded", "score": 1.0, "error": None})

            _run_report(root, "a", final_solver, required=False)

            report = (root / "FINAL_REPORT.md").read_text()
            validation, hidden = report.split("### Hidden performance")
            self.assertIn("| integrity | succeeded | 1.000000 |", validation)
            self.assertIn("| integrity | skipped | not evaluated | 1.0 | disabled |", hidden)
            self.assertIn("ESTABLISHED at the validation stage", report)
            self.assertNotIn("NOT ESTABLISHED", report)

    def test_review_of_a_different_tree_or_failed_review_is_never_claimed(self) -> None:
        for case in ("code_changed_after_review", "review_failed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = _worker_session(root, edit_after_explaining=False)
                final_solver = root / "final_solver"
                shutil.copytree(workspace, final_solver)
                status = "failed" if case == "review_failed" else "succeeded"
                self._assessments(root, tree_hash(final_solver),
                                  {"status": status, "score": None, "error": "probe"})
                if case == "code_changed_after_review":
                    (final_solver / "solver.py").write_text(_SEED + "# unreviewed edit\n")

                _run_report(root, "a", final_solver, required=False)

                report = (root / "FINAL_REPORT.md").read_text()
                self.assertIn("NOT ESTABLISHED for the delivered tree", report)
                self.assertNotIn("ESTABLISHED at the validation stage", report)


if __name__ == "__main__":
    unittest.main()
