from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import numpy as np

from optiprofiler_evolve.criteria.performance.competitors import (
    competitor_names,
    get_competitor,
    materialize_competitor,
)
from optiprofiler_evolve.solver import InterfaceSpec


class CompetitorRegistryTests(unittest.TestCase):
    def test_registry_exposes_the_supported_four_problem_type_matrix(self) -> None:
        self.assertIn("scipy_nelder_mead", competitor_names("u"))
        self.assertNotIn("scipy_nelder_mead", competitor_names("b"))
        self.assertEqual(get_competitor("scipy_powell").supported_ptypes, ("u", "b"))
        self.assertEqual(
            get_competitor("prima_lincoa").supported_ptypes,
            ("u", "b", "l"),
        )
        self.assertEqual(
            get_competitor("pdfo").supported_ptypes,
            ("u", "b", "l", "n"),
        )

    def test_every_registered_wrapper_materializes_as_valid_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, name in enumerate(competitor_names()):
                spec = get_competitor(name)
                destination = root / f"reference-{index}"
                materialize_competitor(
                    name,
                    destination=destination,
                    interface=InterfaceSpec.parse("solver.py:solver"),
                    max_eval_factor=20,
                    ptype=spec.supported_ptypes[0],
                )
                source = (destination / "solver.py").read_text(encoding="utf-8")
                compile(source, str(destination / "solver.py"), "exec")
                budget_option = (
                    "maxiter"
                    if name == "scipy_cobyla"
                    else "maxfev"
                )
                self.assertIn(budget_option, source)

    def test_config_schema_reference_names_match_the_registry(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "config.schema.json").read_text(
                encoding="utf-8"
            )
        )
        names = schema["$defs"]["performance_options"]["properties"]["reference"]["enum"]
        self.assertEqual(names, ["initial", *competitor_names()])

    def test_default_evaluator_image_installs_both_powell_family_packages(self) -> None:
        dockerfile = (
            Path(__file__).resolve().parents[1] / "docker" / "evaluator" / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("ARG NUMPY_VERSION=1.26.4", dockerfile)
        self.assertIn('"pdfo==${PDFO_VERSION}"', dockerfile)
        self.assertIn("https://github.com/libprima/prima.git", dockerfile)

    def test_materializer_rejects_an_incompatible_problem_type_before_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "does not support problem type"):
                materialize_competitor(
                    "prima_newuoa",
                    destination=Path(directory) / "reference",
                    interface=InterfaceSpec.parse("solver.py:solver"),
                    max_eval_factor=20,
                    ptype="b",
                )

    def test_scipy_cobyqa_wrapper_dispatches_bounds_and_budget(self) -> None:
        calls: list[dict[str, object]] = []
        fake_scipy = types.ModuleType("scipy")
        fake_optimize = types.ModuleType("scipy.optimize")

        class Constraint:
            def __init__(self, *args: object) -> None:
                self.args = args

        def minimize(fun, x0, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(x=np.asarray(x0) * 0)

        fake_optimize.Bounds = Constraint
        fake_optimize.LinearConstraint = Constraint
        fake_optimize.NonlinearConstraint = Constraint
        fake_optimize.minimize = minimize
        fake_scipy.optimize = fake_optimize

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reference"
            materialize_competitor(
                "scipy_cobyqa",
                destination=root,
                interface=InterfaceSpec.parse("solver.py:solver"),
                max_eval_factor=20,
                ptype="b",
            )
            spec = importlib.util.spec_from_file_location("competitor", root / "solver.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            with patch.dict(
                sys.modules,
                {"scipy": fake_scipy, "scipy.optimize": fake_optimize},
            ):
                spec.loader.exec_module(module)
                result = module.solver(
                    lambda x: float(np.dot(x, x)),
                    np.array([1.0, 2.0, 3.0]),
                    np.array([-1.0, -1.0, -1.0]),
                    np.array([1.0, 1.0, 1.0]),
                )

        self.assertTrue(np.array_equal(result, np.zeros(3)))
        self.assertEqual(calls[0]["method"], "COBYQA")
        self.assertEqual(calls[0]["options"], {"maxfev": 60})

    def test_scipy_cobyla_uses_its_function_evaluation_budget_option(self) -> None:
        calls: list[dict[str, object]] = []
        fake_scipy = types.ModuleType("scipy")
        fake_optimize = types.ModuleType("scipy.optimize")

        class Constraint:
            def __init__(self, *args: object) -> None:
                self.args = args

        def minimize(fun, x0, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(x=np.asarray(x0))

        fake_optimize.Bounds = Constraint
        fake_optimize.LinearConstraint = Constraint
        fake_optimize.NonlinearConstraint = Constraint
        fake_optimize.minimize = minimize
        fake_scipy.optimize = fake_optimize

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reference"
            materialize_competitor(
                "scipy_cobyla",
                destination=root,
                interface=InterfaceSpec.parse("solver.py:solver"),
                max_eval_factor=20,
                ptype="u",
            )
            spec = importlib.util.spec_from_file_location("competitor", root / "solver.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            with patch.dict(
                sys.modules,
                {"scipy": fake_scipy, "scipy.optimize": fake_optimize},
            ):
                spec.loader.exec_module(module)
                module.solver(
                    lambda x: float(np.dot(x, x)),
                    np.array([1.0, 2.0]),
                )

        self.assertEqual(calls[0]["method"], "COBYLA")
        self.assertEqual(calls[0]["options"], {"maxiter": 40})

    def test_scipy_wrappers_run_each_declared_problem_type(self) -> None:
        from scipy.optimize import OptimizeWarning

        x0 = np.zeros(2)
        bounds = (np.full(2, -2.0), np.full(2, 2.0))
        linear = (
            *bounds,
            np.array([[1.0, 0.0]]),
            np.array([1.5]),
            np.empty((0, 2)),
            np.empty(0),
        )
        nonlinear = (
            *linear,
            lambda x: np.array([x[0] - 1.25]),
            lambda _x: np.empty(0),
        )
        calls = {
            "u": (),
            "b": bounds,
            "l": linear,
            "n": nonlinear,
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in competitor_names():
                if get_competitor(name).family != "scipy":
                    continue
                for ptype in get_competitor(name).supported_ptypes:
                    destination = root / f"{name}-{ptype}"
                    materialize_competitor(
                        name,
                        destination=destination,
                        interface=InterfaceSpec.parse("solver.py:solver"),
                        max_eval_factor=100,
                        ptype=ptype,
                    )
                    spec = importlib.util.spec_from_file_location(
                        f"competitor_{name}_{ptype}",
                        destination / "solver.py",
                    )
                    assert spec is not None and spec.loader is not None
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", OptimizeWarning)
                        result = module.solver(
                            lambda x: float(np.sum((np.asarray(x) - 1.0) ** 2)),
                            x0,
                            *calls[ptype],
                        )
                    self.assertTrue(np.all(np.isfinite(result)), (name, ptype, result))

    def test_optional_powell_wrappers_run_each_declared_problem_type(self) -> None:
        available_families = {
            family
            for family in ("pdfo", "prima")
            if importlib.util.find_spec(family) is not None
        }
        if not available_families:
            self.skipTest("Neither optional PDFO nor independent PRIMA is installed.")

        x0 = np.zeros(2)
        bounds = (np.full(2, -2.0), np.full(2, 2.0))
        linear = (
            *bounds,
            np.array([[1.0, 0.0]]),
            np.array([1.5]),
            np.empty((0, 2)),
            np.empty(0),
        )
        nonlinear = (
            *linear,
            lambda x: np.array([x[0] - 1.25]),
            lambda _x: np.empty(0),
        )
        calls = {"u": (), "b": bounds, "l": linear, "n": nonlinear}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in competitor_names():
                competitor = get_competitor(name)
                if competitor.family not in available_families:
                    continue
                for ptype in competitor.supported_ptypes:
                    destination = root / f"{name}-{ptype}"
                    materialize_competitor(
                        name,
                        destination=destination,
                        interface=InterfaceSpec.parse("solver.py:solver"),
                        max_eval_factor=100,
                        ptype=ptype,
                    )
                    spec = importlib.util.spec_from_file_location(
                        f"competitor_{name}_{ptype}",
                        destination / "solver.py",
                    )
                    assert spec is not None and spec.loader is not None
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    result = module.solver(
                        lambda x: float(np.sum((np.asarray(x) - 1.0) ** 2)),
                        x0,
                        *calls[ptype],
                    )
                    self.assertTrue(np.all(np.isfinite(result)), (name, ptype, result))

    def test_prima_cobyla_wrapper_dispatches_nonlinear_constraints(self) -> None:
        calls: list[dict[str, object]] = []
        constraint_evaluations = {"cub": 0, "ceq": 0}
        fake_prima = types.ModuleType("prima")

        class Constraint:
            def __init__(self, *args: object) -> None:
                self.args = args

        def minimize(fun, x0, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(x=np.asarray(x0) * 0)

        fake_prima.LinearConstraint = Constraint
        fake_prima.NonlinearConstraint = Constraint
        fake_prima.minimize = minimize

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reference"
            materialize_competitor(
                "prima_cobyla",
                destination=root,
                interface=InterfaceSpec.parse("solver.py:solver"),
                max_eval_factor=20,
                ptype="n",
            )
            spec = importlib.util.spec_from_file_location("competitor", root / "solver.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"prima": fake_prima}):
                spec.loader.exec_module(module)
                x0 = np.array([1.0, 2.0])

                def cub(x):
                    constraint_evaluations["cub"] += 1
                    return np.array([x[0] - 1])

                def ceq(x):
                    constraint_evaluations["ceq"] += 1
                    return np.empty(0)

                module.solver(
                    lambda x: float(np.dot(x, x)),
                    x0,
                    np.array([-1.0, -1.0]),
                    np.array([1.0, 1.0]),
                    np.empty((0, 2)),
                    np.empty(0),
                    np.empty((0, 2)),
                    np.empty(0),
                    cub,
                    ceq,
                )

        self.assertEqual(calls[0]["method"], "cobyla")
        self.assertEqual(calls[0]["options"], {"maxfev": 40, "quiet": True})
        self.assertEqual(len(calls[0]["constraints"]), 2)
        self.assertEqual(constraint_evaluations, {"cub": 0, "ceq": 0})


if __name__ == "__main__":
    unittest.main()
