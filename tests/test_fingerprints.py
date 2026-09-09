from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_config import minimal_config

from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.data import resolve_data_plan
from optiprofiler_evolve.phases.prepare import build_experiment_fingerprints
from optiprofiler_evolve.solver import InterfaceSpec


class ExperimentFingerprintTests(unittest.TestCase):
    def test_config_and_code_fingerprints_change_independently(self) -> None:
        config = load_config(minimal_config())
        data = resolve_data_plan(config.data)
        interface = InterfaceSpec.parse("solver.py:solver")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "solver"
            source.mkdir()
            solver = source / "solver.py"
            solver.write_text("def solver(fun, x0):\n    return x0\n", encoding="utf-8")
            first = build_experiment_fingerprints(
                config=config,
                data=data,
                source_tree=source,
                interface=interface,
                runtime="python",
                editable=(".",),
            )

            solver.write_text("def solver(fun, x0):\n    return x0 + 1\n", encoding="utf-8")
            code_changed = build_experiment_fingerprints(
                config=config,
                data=data,
                source_tree=source,
                interface=interface,
                runtime="python",
                editable=(".",),
            )
            config_changed = build_experiment_fingerprints(
                config=config.with_iterations(2),
                data=data,
                source_tree=source,
                interface=interface,
                runtime="python",
                editable=(".",),
            )

        self.assertEqual(first.config_fingerprint, code_changed.config_fingerprint)
        self.assertNotEqual(first.code_fingerprint, code_changed.code_fingerprint)
        self.assertNotEqual(code_changed.config_fingerprint, config_changed.config_fingerprint)
        self.assertEqual(code_changed.code_fingerprint, config_changed.code_fingerprint)
        self.assertEqual(first.universe_fingerprint, data.universe_fingerprint)
        self.assertEqual(first.split_fingerprint, data.split_fingerprint)


if __name__ == "__main__":
    unittest.main()
