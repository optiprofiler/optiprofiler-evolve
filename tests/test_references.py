from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
                    backend="unsafe_local",
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

    def test_prima_newuoa_reference_uses_independent_prima_with_bounded_budget(self) -> None:
        calls: list[dict[str, object]] = []
        fake_prima = types.ModuleType("prima")

        def fake_minimize(fun, x0, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(x=np.zeros_like(x0))

        fake_prima.minimize = fake_minimize
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
                    backend="unsafe_local",
                    reference="prima_newuoa",
                    benchmark={"max_eval_factor": 20},
                ),
            )
            spec = importlib.util.spec_from_file_location(
                "trusted_prima_reference", reference / "solver.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"prima": fake_prima}):
                spec.loader.exec_module(module)
                solution = module.solver(
                    lambda x: float(np.dot(x, x)), np.array([2.0, -3.0, 1.0])
                )
            self.assertTrue(np.array_equal(solution, np.zeros(3)))
            self.assertEqual(calls[0]["method"], "newuoa")
            self.assertEqual(calls[0]["options"], {"maxfun": 60, "quiet": True})


if __name__ == "__main__":
    unittest.main()
