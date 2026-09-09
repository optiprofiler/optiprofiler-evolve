from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.agents.explanations import capture_solver_explanation, explanation_memory


class SolverExplanationTests(unittest.TestCase):
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
