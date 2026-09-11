from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from optiprofiler_evolve.eval_reports import (
    CORE_SCHEMA_REVISION,
    EvalReportError,
    load_eval_report,
)


FIXTURE = Path(__file__).parent / "fixtures" / "eval_report_v1"


def _copy_fixture(root: Path) -> Path:
    destination = root / "evidence"
    shutil.copytree(FIXTURE, destination)
    return destination / "eval_report.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _refresh_companion_receipt(report_path: Path) -> None:
    report = _read(report_path)
    companion_path = report_path.parent / report["plot_data"]["path"]
    payload = companion_path.read_bytes()
    report["plot_data"]["bytes"] = len(payload)
    report["plot_data"]["sha256"] = hashlib.sha256(payload).hexdigest()
    _write(report_path, report)


class EvalReportConsumerTests(unittest.TestCase):
    def test_pinned_core_report_validates_and_supports_bounded_lookup(self) -> None:
        self.assertEqual(
            CORE_SCHEMA_REVISION,
            "bf9d61c1482da6f0ad46fab660270cc8484d9f6f",
        )
        schema_root = files("optiprofiler_evolve.schemas")
        self.assertEqual(
            hashlib.sha256(
                schema_root.joinpath("eval_report.schema.json").read_bytes()
            ).hexdigest(),
            "9b8f76fd9b2d3eff323304b758ff61c5c456d35b1d81fcf05d3ab9d64453a8a3",
        )
        self.assertEqual(
            hashlib.sha256(schema_root.joinpath("plot_data.schema.json").read_bytes()).hexdigest(),
            "76f83e1b7922a1479d8770888bd417c86fe5f837a450789891618960582d323c",
        )
        with tempfile.TemporaryDirectory() as directory:
            bundle = load_eval_report(_copy_fixture(Path(directory)))

            self.assertEqual(bundle.main["schema"], "optiprofiler.eval_report/1")
            self.assertEqual(bundle.companion["schema"], "optiprofiler.plot_data/1")
            history = bundle.history("history-1-1-1")
            self.assertEqual(history["problem_id"], bundle.main["problems"][0]["id"])
            plot = bundle.plot("history-plot-1-objective-cummin")
            self.assertEqual(plot["kind"], "history")

    def test_missing_or_tampered_companion_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            report = _read(report_path)
            companion = report_path.parent / report["plot_data"]["path"]
            companion.unlink()
            with self.assertRaisesRegex(EvalReportError, "companion.*missing"):
                load_eval_report(report_path)

        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            report = _read(report_path)
            report["plot_data"]["sha256"] = "0" * 64
            _write(report_path, report)
            with self.assertRaisesRegex(EvalReportError, "SHA256"):
                load_eval_report(report_path)

    def test_wrong_schema_identity_or_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            report = _read(report_path)
            report["schema"] = "unknown/report"
            _write(report_path, report)
            with self.assertRaisesRegex(EvalReportError, "schema"):
                load_eval_report(report_path)

        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            report = _read(report_path)
            companion_path = report_path.parent / report["plot_data"]["path"]
            companion = _read(companion_path)
            companion["evaluation_id"] = "different-evaluation"
            _write(companion_path, companion)
            _refresh_companion_receipt(report_path)
            with self.assertRaisesRegex(EvalReportError, "evaluation_id"):
                load_eval_report(report_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = _copy_fixture(root)
            report = _read(report_path)
            companion = report_path.parent / report["plot_data"]["path"]
            outside = root / "outside.json"
            companion.replace(outside)
            report["plot_data"]["path"] = "../outside.json"
            payload = outside.read_bytes()
            report["plot_data"]["bytes"] = len(payload)
            report["plot_data"]["sha256"] = hashlib.sha256(payload).hexdigest()
            _write(report_path, report)
            with self.assertRaisesRegex(EvalReportError, "escapes"):
                load_eval_report(report_path)

    def test_unfinished_report_fails_but_rendering_failure_does_not_erase_scores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            report = _read(report_path)
            report["status"] = "running"
            _write(report_path, report)
            with self.assertRaisesRegex(EvalReportError, "not finalized"):
                load_eval_report(report_path)

        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            report = _read(report_path)
            report["status"] = "partial"
            report["stages"]["rendering"] = {
                "status": "failed",
                "reason": "synthetic renderer failure",
            }
            _write(report_path, report)
            bundle = load_eval_report(report_path)

            self.assertTrue(bundle.numerical_success)
            self.assertEqual(bundle.main["stages"]["rendering"]["status"], "failed")
            self.assertIsNotNone(bundle.main["scores"])

    def test_size_limits_are_enforced_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            with self.assertRaisesRegex(EvalReportError, "size limit"):
                load_eval_report(report_path, max_main_bytes=100)


if __name__ == "__main__":
    unittest.main()
