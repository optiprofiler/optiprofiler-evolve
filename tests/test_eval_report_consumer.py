from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from optiprofiler_evolve.observability.eval_reports import (
    EvalReportError,
    UnsupportedEvalReport,
    load_eval_report,
    private_eval_report_paths,
)
from optiprofiler_evolve.observability.schemas import SCHEMA_DIGESTS

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
        schema_root = files("optiprofiler_evolve.observability.schemas")
        for name, digest in SCHEMA_DIGESTS.items():
            self.assertEqual(hashlib.sha256(schema_root.joinpath(name).read_bytes()).hexdigest(), digest)
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

            self.assertTrue(bundle.main["scores"])
            self.assertEqual(bundle.main["stages"]["rendering"]["status"], "failed")
            self.assertIsNotNone(bundle.main["scores"])

    def test_size_limits_are_enforced_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = _copy_fixture(Path(directory))
            with self.assertRaisesRegex(EvalReportError, "size limit"):
                load_eval_report(report_path, max_main_bytes=100)


class EvalReportBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = _copy_fixture(self.root)
        self.main = _read(self.path)
        self.companion_path = self.path.parent / self.main["plot_data"]["path"]
        self.companion = _read(self.companion_path)

    def save(self):
        _write(self.companion_path, self.companion)
        _write(self.path, self.main)
        _refresh_companion_receipt(self.path)

    def assert_semantic_failure(self, reason):
        self.save()
        # These mutations pass JSON Schema; failures must come from cross-field checks.
        for name, value in (("eval_report", _read(self.path)), ("plot_data", self.companion)):
            resource = files("optiprofiler_evolve.observability.schemas").joinpath(f"{name}.schema.json")
            Draft202012Validator(json.loads(resource.read_text())).validate(value)
        with self.assertRaisesRegex(EvalReportError, reason):
            load_eval_report(self.path)

    def add_target_work(self):
        self.main["scores"]["axis_values"]["tolerance"] = [0.1]
        work = {
            "id": "work-1", "tolerance_index": 1, "tolerance": 0.1,
            "axes": ["problem", "solver", "run"],
            "axis_values": {"problem": [self.main["problems"][0]["id"]], "solver": [1, 2], "run": [1]},
            "history": [[[1.0], [2.0]]], "output": [[[1.0], [2.0]]],
        }
        self.companion["target_work"] = [work]
        return work

    def test_unknown_versions_are_distinct_from_broken_reports(self):
        self.main["schema"] = "optiprofiler.eval_report/2"
        _write(self.path, self.main)
        for read in (load_eval_report, private_eval_report_paths):
            with self.assertRaises(UnsupportedEvalReport):
                read(self.path)
        self.main["schema"] = "optiprofiler.eval_report/1"
        self.main["scores"] = "broken"
        _write(self.path, self.main)
        with self.assertRaises(EvalReportError) as failure:
            load_eval_report(self.path)
        self.assertNotIsInstance(failure.exception, UnsupportedEvalReport)
        # Feedback only needs the valid receipt, not unrelated score metadata.
        self.assertEqual(private_eval_report_paths(self.path), {self.path, self.companion_path})

    def test_available_channel_needs_a_representation(self):
        self.companion["histories"][0]["channels"]["objective"]["representation"] = None
        self.assert_semantic_failure("representation")

    def test_archive_does_not_apply_original_selection_arithmetic(self):
        self.main["coverage"].update(scope="retained_archive", selected=0, original_selection_known=False)
        self.save()
        self.assertIsNotNone(load_eval_report(self.path).main["scores"])

    def test_target_work_can_declare_a_subset_solver_axis(self):
        work = self.add_target_work()
        work["axis_values"]["solver"] = [2]
        work["history"] = [[[2.0]]]
        work["output"] = [[[2.0]]]
        self.save()
        self.assertEqual(load_eval_report(self.path).target_work("work-1"), work)

    def test_display_series_need_not_cover_every_solver(self):
        for plot in self.companion["plots"]:
            plot["series"] = []
        self.save()
        self.assertEqual(load_eval_report(self.path).plot(self.companion["plots"][0]["id"])["series"], [])

    def test_main_and_companion_symlinks_are_rejected(self):
        alias = self.path.with_name("alias.json")
        alias.symlink_to(self.path.name)
        with self.assertRaisesRegex(EvalReportError, "symlink"):
            load_eval_report(alias)
        original = self.companion_path.with_suffix(".original")
        self.companion_path.rename(original)
        self.companion_path.symlink_to(original.name)
        with self.assertRaisesRegex(EvalReportError, "symlink"):
            load_eval_report(self.path)

    def test_companion_directory_symlink_is_rejected_even_when_contained(self):
        alias = self.path.parent / "alias"
        alias.symlink_to(self.path.parent, target_is_directory=True)
        self.main["plot_data"]["path"] = f"alias/{self.companion_path.name}"
        _write(self.path, self.main)
        with self.assertRaisesRegex(EvalReportError, "symlink"):
            load_eval_report(self.path)

    def test_authorized_parent_may_use_a_system_directory_alias(self):
        alias = self.root / "parent_alias"
        alias.symlink_to(self.path.parent, target_is_directory=True)
        self.assertTrue(load_eval_report(alias / self.path.name).main["scores"])

    def test_nonregular_main_is_rejected_without_opening(self):
        directory = self.root / "directory.json"
        directory.mkdir()
        with self.assertRaisesRegex(EvalReportError, "regular file"):
            load_eval_report(directory)
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo.json"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(EvalReportError, "regular file"):
                load_eval_report(fifo)

    def test_read_is_bounded_even_if_file_grows_after_fstat(self):
        # A real file exceeds the limit while fstat reports its earlier, smaller size.
        with patch(
            "optiprofiler_evolve.files.os.fstat",
            return_value=SimpleNamespace(st_mode=self.path.stat().st_mode, st_size=0),
        ):
            with self.assertRaisesRegex(EvalReportError, "size limit"):
                load_eval_report(self.path, max_main_bytes=100)

    def test_companion_limit_and_exact_size_boundary(self):
        with self.assertRaisesRegex(EvalReportError, "size limit"):
            load_eval_report(self.path, max_companion_bytes=100)
        self.assertTrue(load_eval_report(
            self.path, max_main_bytes=self.path.stat().st_size,
            max_companion_bytes=self.companion_path.stat().st_size,
        ).main["scores"])

    def test_io_and_invalid_json_errors_are_report_errors(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(EvalReportError):
                load_eval_report(self.path)
        for raw in (b"\xff", b"{", b"[]", b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(EvalReportError):
                    load_eval_report(self.path)

    def test_receipt_discovery_does_not_read_numeric_companion(self):
        self.companion_path.unlink()
        self.assertEqual(private_eval_report_paths(self.path), {self.path, self.companion_path})
        with self.assertRaisesRegex(EvalReportError, "missing"):
            load_eval_report(self.path)

    def test_sample_count_and_coordinate_lengths_are_checked(self):
        self.companion["histories"][0]["channels"]["objective"]["count"] = 0
        self.assert_semantic_failure("sample count")

    def test_plot_coordinate_lengths_are_checked(self):
        self.companion["plots"][0]["series"][0]["x"].append(3)
        self.assert_semantic_failure("coordinate arrays")

    def test_duplicate_and_orphan_records_are_rejected(self):
        self.companion["histories"].append(self.companion["histories"][0])
        self.assert_semantic_failure("Duplicate history")

    def test_unknown_history_reference_is_rejected(self):
        self.main["problems"][0]["runs"][0]["history_ref"] = "missing"
        self.assert_semantic_failure("unknown history")

    def test_target_work_shapes_and_id_lookup(self):
        work = self.add_target_work()
        self.save()
        bundle = load_eval_report(self.path)
        self.assertEqual(bundle.target_work("work-1"), work)
        for lookup in (bundle.history, bundle.plot, bundle.target_work):
            with self.assertRaises(KeyError):
                lookup("unknown")
        work["output"][0][0].append(2)
        self.assert_semantic_failure("solver/run dimensions")

    def test_target_work_problem_and_tolerance_references(self):
        work = self.add_target_work()
        work["axis_values"]["problem"] = ["unknown"]
        self.assert_semantic_failure("primary problems")

    def test_target_work_tolerance_reference_is_checked(self):
        self.add_target_work()["tolerance"] = 0.2
        self.assert_semantic_failure("tolerance")

    def test_schema_integer_encoded_as_float_can_index_tolerance(self):
        self.add_target_work()["tolerance_index"] = 1.0
        self.save()
        self.assertTrue(load_eval_report(self.path).main["scores"])

    def test_explicit_problem_axis_order_is_not_replaced_by_main_order(self):
        work = self.add_target_work()
        second = deepcopy(self.main["problems"][0])
        second.update(id="second", name="second")
        second["plot_refs"] = []
        for run in second["runs"]:
            run["history_ref"] = None
        self.main["problems"].append(second)
        self.main["coverage"].update(selected=2, loaded=2, completed=2)
        work["axis_values"]["problem"].insert(0, "second")
        for kind in ("history", "output"):
            work[kind].insert(0, [[3.0], [4.0]])
        self.save()
        loaded = load_eval_report(self.path).target_work("work-1")
        self.assertEqual(loaded["axis_values"]["problem"][0], "second")
        self.assertEqual(loaded["history"][0], [[3.0], [4.0]])

    def test_profile_and_log_ratio_coordinates_and_bar_identity(self):
        self.add_target_work()
        profile = {
            "id": "profile", "kind": "performance", "tolerance_index": 1,
            "history_or_output": "history", "status": "numeric_prepared",
            "fidelity": "exact_rendered_data", "observation_scope": "scoring_profile_work",
            "target_work_ref": "work-1", "series": [{
                "solver_index": 1, "x": [1.0], "mean": [1.0], "lower": [1.0], "upper": [1.0],
                "geometry": "step", "band_visible": False,
            }], "x_transform": "work/best_work", "ratio_max": 2.0,
            "failure_placeholder": 2.2, "x_limits": [0.0, 2.2], "y_limits": [0, 1],
            "errorbar_type": "minmax", "std_ddof": 0, "n_runs": 1,
        }
        ratio = {
            "id": "ratio", "kind": "log_ratio", "tolerance_index": 1,
            "history_or_output": "history", "status": "numeric_prepared",
            "fidelity": "exact_rendered_data", "observation_scope": "scoring_profile_work",
            "target_work_ref": "work-1", "series": [{
                "geometry": "bar", "x": [1.0], "y": [-1.0], "visible": [True], "opacity": [1],
                "solver_index_by_sign": {"negative": 1, "positive": 2},
            }], "bar_groups": [{"start_index": 1, "end_index": 1, "solver_index": 1, "opacity": 1}],
            "ratio_max": 2.0, "failure_placeholder": 2.2,
            "x_transform": "sorted_bar_position", "y_transform": "log2(work_solver_1/work_solver_2)",
            "x_limits": [0.0, 2.0], "y_limits": [-1.0, 1.0], "both_failed_pairs": 0, "tie_pairs": 0,
            "problem_mapping": None, "problem_mapping_reason": "legacy_bar_sort_does_not_retain_identity",
        }
        self.companion["plots"].extend((profile, ratio))
        self.main["plot_data"]["plot_count"] += 2
        self.main["profiles"]["plot_refs"] = ["profile", "ratio"]
        self.save()
        self.assertTrue(load_eval_report(self.path).main["scores"])
        # MATLAB retains bar identities; Python legitimately has no such mapping.
        ratio.pop("problem_mapping_reason")
        ratio["problem_mapping"] = "bar_sources"
        ratio["bar_sources"] = {
            "problem_index": [1], "run_index": [1], "solver1_failed": [False],
            "solver2_failed": [False], "tie": [False],
        }
        self.save()
        self.assertTrue(load_eval_report(self.path).main["scores"])
        ratio["bar_sources"]["problem_index"] = [2]
        self.assert_semantic_failure("unknown problem index")

    def test_profile_score_tensor_lengths(self):
        self.main["scores"]["axis_values"].update(tolerance=[0.1], profile_type=["performance", "data"])
        self.main["scores"]["profile_scores"] = [[[[0.5, 0.5], [0.5, 0.5]]]]
        self.assert_semantic_failure("solver axis")

    def test_run_identity_and_coverage_are_checked(self):
        self.main["problems"][0]["runs"].append(self.main["problems"][0]["runs"][0])
        self.assert_semantic_failure("Duplicate run")

    def test_coverage_cannot_claim_more_completions_than_loads(self):
        self.main["coverage"]["completed"] = 2
        self.assert_semantic_failure("coverage")

    def test_receipt_bytes_and_nonfinal_numerics_fail_closed(self):
        self.main["plot_data"]["bytes"] += 1
        _write(self.path, self.main)
        with self.assertRaisesRegex(EvalReportError, "byte count"):
            load_eval_report(self.path)
        self.save()
        self.main = _read(self.path)
        self.main["stages"]["numerical"]["status"] = "failed"
        _write(self.path, self.main)
        with self.assertRaisesRegex(EvalReportError, "numerical and scoring"):
            load_eval_report(self.path)

    def test_binned_history_intervals_and_extrema(self):
        channel = self.companion["histories"][0]["channels"]["objective"]
        for other in self.companion["histories"][0]["channels"].values():
            other["total_evaluations"] = 65
        self.main["problems"][0]["runs"][0]["evaluations"] = 65
        channel.update(count=65, representation="bin_extrema", values=None, bins=[
            {
                "start_index": i * 65 // 32 + 1, "end_index": (i + 1) * 65 // 32,
                "first": 1.0, "last": 1.0,
                "finite_min": {"value": 1.0, "evaluation_index": i * 65 // 32 + 1},
                "finite_max": {"value": 1.0, "evaluation_index": i * 65 // 32 + 1},
                "nonfinite": {"nan": 0, "positive_infinity": 0, "negative_infinity": 0},
            } for i in range(32)
        ])
        self.save()
        self.assertTrue(load_eval_report(self.path).main["scores"])
        channel["bins"][0]["finite_min"]["evaluation_index"] = 10
        self.assert_semantic_failure("extremum index")

    def test_matlab_renderer_variants_do_not_change_numeric_success(self):
        self.companion["semantics"]["renderer_variants"] = {"history": {"native": "line"}}
        for plot in self.companion["plots"]:
            plot["renderer_variants_ref"] = "history"
        self.save()
        self.assertTrue(load_eval_report(self.path).main["scores"])
        self.companion["semantics"]["renderer_variants"] = {}
        self.assert_semantic_failure("renderer variant")


if __name__ == "__main__":
    unittest.main()
