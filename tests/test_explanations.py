from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from optiprofiler_evolve.agents.explanations import capture_solver_explanation
from optiprofiler_evolve.observability.attempts import explanation_memory


class SolverExplanationTests(unittest.TestCase):
    def test_final_explanation_repair_preserves_controller_cancellation(self):
        from optiprofiler_evolve.errors import RunCancelled
        from optiprofiler_evolve.models import FinalistResult
        from optiprofiler_evolve.phases.report import ReportContext, ReportPhase
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solver = root / "final_solver"
            solver.mkdir()
            (solver / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            def cancelled(_):
                raise RunCancelled("owner stopped run")
            with self.assertRaises(RunCancelled):
                ReportPhase(require_explanation=True)._ensure_explanation(ReportContext(
                    run_dir=root, options={}, artifacts={}, emit=lambda *a, **k: None,
                    services=SimpleNamespace(run_trusted_agent=cancelled),
                    final_result=FinalistResult("candidate", 0, 0.5, 0.5, 0.5, root),
                    final_solver=solver))

    def test_explainer_cannot_deliver_explanation_for_modified_solver_copy(self):
        from optiprofiler_evolve.models import FinalistResult
        from optiprofiler_evolve.phases.report import ReportContext, ReportPhase
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solver = root / "final_solver"
            solver.mkdir()
            code = "def solver(fun, x0):\n    return x0\n"
            (solver / "solver.py").write_text(code)
            def changed(job):
                workspace = root / "author"
                shutil.copytree(job.inputs["candidate"], workspace / "candidate")
                (workspace / "candidate/solver.py").write_text(code + "# changed\n")
                return SimpleNamespace(workspace=workspace)
            delivery = ReportPhase(require_explanation=True)._ensure_explanation(ReportContext(
                run_dir=root, options={}, artifacts={}, emit=lambda *a, **k: None,
                services=SimpleNamespace(run_trusted_agent=changed),
                final_result=FinalistResult("candidate", 0, 0.5, 0.5, 0.5, root), final_solver=solver))
            self.assertEqual(delivery["explanation_status"], "invalid")
            self.assertIn("changed its solver copy", delivery["explanation_error"])
            self.assertEqual((solver / "solver.py").read_text(), code)

    def test_final_report_repairs_missing_explanation_without_changing_solver(self):
        from optiprofiler_evolve.models import FinalistResult
        from optiprofiler_evolve.phases.report import ReportContext, ReportPhase
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solver = root / "final_solver"
            solver.mkdir()
            code = "def solver(fun, x0):\n    return x0\n"
            (solver / "solver.py").write_text(code)
            (root / "candidates/seed").mkdir(parents=True)
            (root / "candidates/seed/solver.py").write_text(code)
            jobs = []
            def run_job(job):
                jobs.append(job)
                workspace = root / "author"
                shutil.copytree(job.inputs["candidate"], workspace / "candidate")
                meta = workspace / "candidate/.optiprofiler_evolve"
                meta.mkdir()
                (meta / "solver_explanation.json").write_text(json.dumps({
                    "schema": "solver_explanation/1", "summary": "Return the starting point.",
                    "pseudocode": ["Return x0."], "mechanisms": [{"name": "identity",
                    "description": "No objective calls.", "code_bindings": [{"file": "solver.py", "lines": [1, 2]}]}]}))
                return SimpleNamespace(workspace=workspace, success=True)
            best = FinalistResult("candidate-1", 0, 0.5, 0.5, 0.5, root)
            result = ReportPhase(require_explanation=True).run(ReportContext(
                run_dir=root, options={}, artifacts={}, emit=lambda *a, **k: None,
                services=SimpleNamespace(run_trusted_agent=run_job),
                final_result=best, final_solver=solver))
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(jobs), 1)
            self.assertEqual((solver / "solver.py").read_text(), code)
            self.assertIn("Pseudocode", (root / "FINAL_SOLVER_EXPLANATION.md").read_text())
            self.assertEqual(json.loads((root / "final_delivery.json").read_text())["explanation_status"], "complete")

    def test_required_explanation_failure_does_not_claim_delivery_or_delete_code(self):
        from optiprofiler_evolve.models import FinalistResult
        from optiprofiler_evolve.phases.report import ReportContext, ReportPhase
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solver = root / "final_solver"
            solver.mkdir()
            code = "def solver(fun, x0):\n    return x0\n"
            (solver / "solver.py").write_text(code)
            (root / "candidates/seed").mkdir(parents=True)
            (root / "candidates/seed/solver.py").write_text(code)
            def failed(_):
                raise RuntimeError("provider unavailable")
            result = ReportPhase(require_explanation=True).run(ReportContext(
                run_dir=root, options={}, artifacts={}, emit=lambda *a, **k: None,
                services=SimpleNamespace(run_trusted_agent=failed),
                final_result=FinalistResult("candidate", 0, 0.5, 0.5, 0.5, root), final_solver=solver))
            self.assertEqual(result.status, "failed")
            self.assertEqual((solver / "solver.py").read_text(), code)
            self.assertIn("provider unavailable", (root / "final_delivery.json").read_text())

    def test_unsafe_explanation_is_rejected_without_removing_evidence(self):
        for kind in ("link", "parent_link", "dangling_link", "fifo", "oversized"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                metadata = workspace / ".optiprofiler_evolve"
                metadata.mkdir(parents=True)
                source = metadata / "solver_explanation.json"
                outside = root / "outside.json"
                outside.write_text('{"summary": "private sentinel"}')
                if kind == "parent_link":
                    metadata.rmdir()
                    metadata.symlink_to(root, target_is_directory=True)
                elif kind == "link":
                    source.symlink_to(outside)
                elif kind == "dangling_link":
                    source.symlink_to(root / "absent")
                elif kind == "fifo":
                    os.mkfifo(source)
                else:
                    source.write_bytes(b" " * (1024 * 1024 + 1))
                destination = root / "captured.json"
                with self.assertRaises((ValueError, OSError)):
                    capture_solver_explanation(workspace, destination, attempt_id="probe")
                self.assertFalse(destination.exists())
                self.assertTrue(metadata.is_symlink() or metadata.exists())
                self.assertEqual(outside.read_text(), '{"summary": "private sentinel"}')

    def test_valid_worker_explanation_is_normalized_and_removed_from_solver_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            metadata = workspace / ".optiprofiler_evolve"
            metadata.mkdir(parents=True)
            (workspace / "solver.py").write_text(
                "def solver(fun, x0):\n    return x0\n", encoding="utf-8"
            )
            (metadata / "solver_explanation.json").write_text(
                json.dumps(
                    {
                        "schema": "solver_explanation/1",
                        "summary": "Use adaptive polling after unsuccessful steps.",
                        "pseudocode": ["poll", "accept improvement", "shrink otherwise"],
                        "mechanisms": [
                            {
                                "name": "adaptive polling",
                                "description": "Changes the poll radius using observed progress.",
                                "code_bindings": [
                                    {"file": "solver.py", "lines": [1, 2]}
                                ],
                            }
                        ],
                        "expected_strengths": ["low-dimensional smooth problems"],
                        "known_limitations": ["not designed for constraints"],
                        "evidence_consulted": ["smoke/result.json"],
                    }
                ),
                encoding="utf-8",
            )

            output = capture_solver_explanation(
                workspace,
                root / "explanations" / "it001-i00-a00.json",
                attempt_id="it001-i00-a00",
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["mechanisms"][0]["name"], "adaptive polling")
            self.assertFalse((workspace / ".optiprofiler_evolve").exists())
            self.assertEqual(
                explanation_memory(output),
                {
                    "summary": "Use adaptive polling after unsuccessful steps.",
                    "mechanisms": ["adaptive polling"],
                    "pseudocode": ["poll", "accept improvement", "shrink otherwise"],
                },
            )

    def test_missing_explanation_is_recorded_without_rejecting_the_solver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()

            output = capture_solver_explanation(
                workspace,
                root / "explanations" / "attempt.json",
                attempt_id="attempt",
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "missing")
            self.assertIn("did not produce", payload["error"])

    def test_invalid_explanation_is_quarantined_as_metadata_not_candidate_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            metadata = workspace / ".optiprofiler_evolve"
            metadata.mkdir(parents=True)
            (metadata / "solver_explanation.json").write_text(
                '{"schema":"wrong","summary":"x"}', encoding="utf-8"
            )

            output = capture_solver_explanation(
                workspace,
                root / "explanations" / "attempt.json",
                attempt_id="attempt",
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "invalid")
            self.assertFalse(metadata.exists())

    def test_code_bindings_must_point_to_real_candidate_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            metadata = workspace / ".optiprofiler_evolve"
            metadata.mkdir(parents=True)
            (workspace / "solver.py").write_text(
                "def solver(fun, x0):\n    return x0\n",
                encoding="utf-8",
            )
            (metadata / "solver_explanation.json").write_text(
                json.dumps(
                    {
                        "schema": "solver_explanation/1",
                        "summary": "Claims a mechanism outside the actual source.",
                        "pseudocode": ["return x0"],
                        "mechanisms": [
                            {
                                "name": "invalid binding",
                                "description": "The declared line does not exist.",
                                "code_bindings": [
                                    {"file": "solver.py", "lines": [1, 99]}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = capture_solver_explanation(
                workspace,
                root / "explanations" / "attempt.json",
                attempt_id="attempt",
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "invalid")
            self.assertIn("exceed", payload["error"])


if __name__ == "__main__":
    unittest.main()
