from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.agents.feedback import project_feedback
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.observability.eval_reports import EvalReportError


class FeedbackProjectionTests(unittest.TestCase):
    def test_raw_reports_and_custom_named_companions_are_not_feedback(self):
        for companion_name in ("eval_report.plot_data.json", "numeric-private.log"):
            with self.subTest(companion=companion_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "private"
                benchmark = source / "benchmark"
                shutil.copytree(Path(__file__).parent / "fixtures" / "eval_report_v1", benchmark)
                original = benchmark / "eval_report.plot_data.json"
                original.rename(benchmark / companion_name)
                main_path = benchmark / "eval_report.json"
                main = json.loads(main_path.read_text())
                main["plot_data"]["path"] = companion_name
                main_path.write_text(json.dumps(main))
                (benchmark / "profile.png").write_bytes(b"public picture")
                (benchmark / "other.json").write_text('{"ordinary": true}')
                destination = root / "feedback"
                result = EvaluationResult("public", 0.5, 0.5, 0.5, 1, source)
                project_feedback(source, destination, result, mode="full_artifacts")
                self.assertFalse((destination / "benchmark" / "eval_report.json").exists())
                self.assertFalse((destination / "benchmark" / companion_name).exists())
                self.assertTrue((destination / "benchmark" / "profile.png").exists())
                self.assertTrue((destination / "benchmark" / "other.json").exists())
                index = (destination / "artifact_index.json").read_text()
                self.assertNotIn(companion_name, index)
                self.assertNotIn("eval_report.json", index)

    def test_bad_receipt_aborts_before_copying_any_benchmark_artifact(self):
        for bad in ("not json", '{"plot_data": null}'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "private"
                benchmark = source / "benchmark"
                benchmark.mkdir(parents=True)
                (benchmark / "a-numeric.json").write_text("private numbers")
                (benchmark / "eval_report.json").write_text(bad)
                destination = root / "feedback"
                result = EvaluationResult("public", 0.5, 0.5, 0.5, 1, source)
                with self.assertRaises(EvalReportError):
                    project_feedback(source, destination, result, mode="full_artifacts")
                self.assertFalse((destination / "benchmark").exists())
                self.assertFalse((destination / "artifact_index.json").exists())

    def test_absent_or_escaping_receipt_is_not_a_legacy_fallback(self):
        for receipt in (None, "escape"):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "private"
                benchmark = source / "benchmark"
                shutil.copytree(Path(__file__).parent / "fixtures" / "eval_report_v1", benchmark)
                main_path = benchmark / "eval_report.json"
                main = json.loads(main_path.read_text())
                if receipt is None:
                    main["plot_data"] = None
                else:
                    main["plot_data"]["path"] = "../private.json"
                main_path.write_text(json.dumps(main))
                result = EvaluationResult("public", 0.5, 0.5, 0.5, 1, source)
                with self.assertRaises(EvalReportError):
                    project_feedback(source, root / "feedback", result, mode="full_artifacts")
                self.assertFalse((root / "feedback" / "benchmark").exists())

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
