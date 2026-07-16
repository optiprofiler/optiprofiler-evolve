from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np

from optiprofiler_evolve.config import EvaluationConfig
from optiprofiler_evolve.references import materialize_reference
from optiprofiler_evolve.solver import InterfaceSpec, validate_interface


class ReferenceTests(unittest.TestCase):
    def test_scipy_powell_reference_is_materialized_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = root / "initial"
            initial.mkdir()
            (initial / "solver.py").write_text(
                "def solver(fun, x0):\n    return x0\n", encoding="utf-8"
            )
            reference = materialize_reference(
                initial=initial,
                destination=root / "reference",
                interface=InterfaceSpec.parse("solver.py:solver"),
                config=EvaluationConfig(
                    backend="local",
                    reference="scipy_powell",
                    benchmark={"max_eval_factor": 20},
                ),
            )
            validate_interface(reference, InterfaceSpec.parse("solver.py:solver"), "python")
            self.assertNotEqual(
                (initial / "solver.py").read_text(encoding="utf-8"),
                (reference / "solver.py").read_text(encoding="utf-8"),
            )
            spec = importlib.util.spec_from_file_location("trusted_reference", reference / "solver.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            solution = module.solver(lambda x: float(np.dot(x, x)), np.array([2.0, -3.0]))
            self.assertLess(np.linalg.norm(solution), 1e-4)


if __name__ == "__main__":
    unittest.main()
