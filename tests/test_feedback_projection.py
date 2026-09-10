from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.agents.feedback import project_feedback
from optiprofiler_evolve.models import EvaluationResult


class FeedbackProjectionTests(unittest.TestCase):
    def test_feedback_modes_expose_distinct_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private"
            benchmark = source / "benchmark"
            benchmark.mkdir(parents=True)
            (source / "private_notes.txt").write_text("controller only", encoding="utf-8")
            (benchmark / "profile.png").write_bytes(b"png")
            (benchmark / "run.log").write_text("safe log", encoding="utf-8")
            (benchmark / "state.pkl").write_bytes(b"private")
            result = EvaluationResult(
                "public",
                0.75,
                0.9,
                0.4,
                3,
                source,
            )

            score_dir = root / "score"
            project_feedback(source, score_dir, result, mode="score_only")

            full_dir = root / "full"
            project_feedback(source, full_dir, result, mode="full_artifacts")

            score_files = sorted(
                path.relative_to(score_dir).as_posix() for path in score_dir.rglob("*")
            )
            self.assertEqual(score_files, ["feedback.md", "result.json"])
            score_result = json.loads((score_dir / "result.json").read_text())
            self.assertNotIn("candidate_score", score_result)
            self.assertNotIn("reference_score", score_result)

            self.assertTrue((full_dir / "benchmark" / "profile.png").is_file())
            self.assertTrue((full_dir / "benchmark" / "run.log").is_file())
            self.assertFalse((full_dir / "benchmark" / "state.pkl").exists())
            self.assertFalse((full_dir / "private_notes.txt").exists())
            self.assertTrue((full_dir / "artifact_index.json").is_file())

    def test_unknown_feedback_mode_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private"
            source.mkdir()
            result = EvaluationResult("public", 0.5, 0.5, 0.5, 1, source)

            with self.assertRaisesRegex(ValueError, "feedback projection mode"):
                project_feedback(
                    source,
                    root / "structured",
                    result,
                    mode="structured_numeric",
                )
            self.assertFalse((root / "structured").exists())


if __name__ == "__main__":
    unittest.main()
