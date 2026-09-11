from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_config import minimal_config

from optiprofiler_evolve.config import ComponentConfig, load_config
from optiprofiler_evolve.controller.experiments import run_matrix
from optiprofiler_evolve.models import EvolveResult


class ExperimentMatrixTests(unittest.TestCase):
    def test_successful_unscored_cells_are_persisted_and_excluded_from_mean(self):
        for scores in ((None, None), (None, 0.0, 2.0, None)):
            with self.subTest(scores=scores), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)

                def fake_run(**kwargs):
                    score = scores[kwargs["config"].evolution.random_seed]
                    return EvolveResult(Path(kwargs["run_dir"]), None, None, None, None, score, ())

                summary = run_matrix(
                    load_config(minimal_config()), {"partial": lambda config: config},
                    seeds=tuple(range(len(scores))), initial=root,
                    run_root=root / "runs", run=fake_run,
                )
                variant = summary["variants"]["partial"]
                self.assertEqual(variant["n_ok"], len(scores))
                self.assertEqual(variant["n_failed"], 0)
                self.assertEqual(variant["n_unscored"], 2)
                self.assertEqual(variant["mean_final_score"], None if len(scores) == 2 else 1.0)
                persisted = json.loads((root / "runs" / "summary.json").read_text())
                self.assertEqual(persisted["variants"]["partial"], variant)

    def test_variants_record_only_their_diff_from_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "solver"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")

            def fake_run(**kwargs: object) -> EvolveResult:
                run_dir = Path(kwargs["run_dir"])
                run_dir.mkdir(parents=True)
                return EvolveResult(run_dir, source, "seed", 0.5, 0.5, 0.5, ())

            base = load_config(minimal_config())
            base = base.with_step(ComponentConfig("static_audit"), after="mutate")
            summary = run_matrix(
                base,
                {
                    "full": lambda config: config,
                    "no_audit": lambda config: config.without_step("static_audit"),
                    "oneshot": lambda config: config.with_iterations(1),
                },
                seeds=(1, 2),
                initial=source,
                run_root=root / "runs",
                run=fake_run,
            )
            self.assertEqual(len(summary["variants"]["no_audit"]["runs"]), 2)
            paths = {item["path"] for item in summary["variants"]["no_audit"]["diff_vs_base"]}
            self.assertTrue(any(path.startswith("workflow.attempt_steps") for path in paths))
            self.assertTrue((root / "runs" / "summary.json").is_file())

    def test_failed_cell_is_recorded_without_losing_completed_cells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "solver"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")

            def flaky_run(**kwargs: object) -> EvolveResult:
                config = kwargs["config"]
                if config.evolution.random_seed == 2:
                    raise RuntimeError("intentional cell failure")
                run_dir = Path(kwargs["run_dir"])
                run_dir.mkdir(parents=True)
                return EvolveResult(run_dir, source, "seed", 0.5, 0.5, 0.5, ())

            summary = run_matrix(
                load_config(minimal_config()),
                {"oneshot": lambda config: config.with_iterations(1)},
                seeds=(1, 2, 3),
                initial=source,
                run_root=root / "runs",
                run=flaky_run,
            )

            variant = summary["variants"]["oneshot"]
            self.assertEqual(variant["n_ok"], 2)
            self.assertEqual(variant["n_failed"], 1)
            self.assertEqual(variant["runs"][1]["status"], "failed")
            self.assertIn("intentional cell failure", variant["runs"][1]["error"])
            persisted = (root / "runs" / "summary.json").read_text(encoding="utf-8")
            self.assertIn("intentional cell failure", persisted)


if __name__ == "__main__":
    unittest.main()
