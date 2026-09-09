from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.solver import (
    InterfaceSpec,
    changed_files,
    copy_initial_source,
    validate_candidate_imports,
    validate_edit_scope,
    validate_interface,
    validate_tree_safety,
)


class SolverTests(unittest.TestCase):
    def test_runtime_detection_and_python_interface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            interface = InterfaceSpec.parse("solver.py:solver")
            self.assertEqual(interface.detect_runtime("auto"), "python")
            validate_interface(root, interface, "python")

    def test_file_initial_is_copied_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.py"
            source.write_text("def solver(fun, x0): return x0\n")
            destination = root / "snapshot"
            copy_initial_source(source, destination)
            self.assertEqual(source.read_text(), (destination / "input.py").read_text())

    def test_edit_scope_and_symlink_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before"
            after = root / "after"
            before.mkdir()
            after.mkdir()
            (before / "solver.py").write_text("old")
            (after / "solver.py").write_text("new")
            (after / "notes.txt").write_text("new")
            changes = changed_files(before, after)
            with self.assertRaisesRegex(ValueError, "outside editable"):
                validate_edit_scope(changes, ["solver.py"])
            (after / "escape").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                validate_tree_safety(after, max_files=20, max_bytes=10000)

    def test_candidate_import_ablation_rejects_static_and_dynamic_solver_apis(self) -> None:
        samples = (
            "from scipy.optimize import minimize\n",
            "import scipy.optimize as so\n",
            "import scipy\nx = scipy.optimize.minimize\n",
            "import importlib\nx = importlib.import_module('pdfo')\n",
        )
        for source in samples:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "solver.py").write_text(source)
                with self.assertRaisesRegex(ValueError, "forbidden by this experiment"):
                    validate_candidate_imports(
                        root,
                        runtime="python",
                        forbidden=("scipy.optimize", "pdfo"),
                    )

    def test_candidate_import_ablation_allows_numerical_building_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solver.py").write_text("import numpy as np\nfrom scipy import linalg\n")
            validate_candidate_imports(
                root,
                runtime="python",
                forbidden=("scipy.optimize", "pdfo"),
            )


if __name__ == "__main__":
    unittest.main()
