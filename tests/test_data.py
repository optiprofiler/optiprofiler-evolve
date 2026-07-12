from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.config import DataConfig, SplitConfig
from optiprofiler_evolve.data import resolve_data_plan, write_data_manifests


class DataPlanTests(unittest.TestCase):
    def test_split_is_deterministic_disjoint_and_smoke_is_public(self) -> None:
        config = DataConfig(
            problem_names=tuple(f"P{index}" for index in range(10)),
            split=SplitConfig(hidden_fraction=0.2, smoke_count=3, seed=42),
        )
        first = resolve_data_plan(config)
        second = resolve_data_plan(config)
        self.assertEqual(first, second)
        self.assertFalse(set(first.public).intersection(first.hidden))
        self.assertTrue(set(first.smoke).issubset(first.public))
        self.assertEqual(set(first.final), set(first.universe))

    def test_worker_manifest_does_not_contain_hidden_names(self) -> None:
        config = DataConfig(
            problem_names=("PUBLIC", "HIDDEN"),
            split=SplitConfig(public=("PUBLIC",), hidden=("HIDDEN",), smoke_count=1),
        )
        plan = resolve_data_plan(config)
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_data_manifests(plan, run_dir)
            visible = (run_dir / "public_data_manifest.json").read_text(encoding="utf-8")
            trusted = (run_dir / "controller" / "data_manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("HIDDEN", visible)
        self.assertIn("HIDDEN", trusted)


if __name__ == "__main__":
    unittest.main()
