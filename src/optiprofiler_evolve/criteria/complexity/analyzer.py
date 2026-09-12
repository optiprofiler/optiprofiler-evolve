"""Static source budgets relative to one frozen initial solver."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path

from optiprofiler_evolve.config import SandboxConfig
from optiprofiler_evolve.files import write_json
from optiprofiler_evolve.solver import source_files, tree_hash, validate_tree_safety

ANALYZER = "radon/6.0.1"
METRICS_VERSION = "source_complexity/2"
ANALYSIS_TIMEOUT = 30


@dataclass(frozen=True)
class ComplexityMetrics:
    decision_complexity: int
    logical_lines: int
    hotspots: tuple[dict[str, object], ...] = ()


class ComplexityChecker:
    """Cache source measurements against one fixed baseline, independent of assessment policy."""

    def __init__(
        self, seed: Path, sandbox: SandboxConfig, output: Path
    ) -> None:
        self.sandbox = sandbox
        self.output = output
        self._lock = threading.Lock()
        self.baseline_hash = tree_hash(seed)
        self.baseline = self._analyze(seed)
        self._cache = {self.baseline_hash: self.baseline}
        write_json(
            output / "baseline.json",
            {
                "schema": METRICS_VERSION,
                "analyzer": ANALYZER,
                "tree_hash": self.baseline_hash,
                "metrics": asdict(self.baseline),
            },
        )

    def _analyze(self, root: Path) -> ComplexityMetrics:
        validate_tree_safety(
            root,
            max_files=self.sandbox.max_candidate_files,
            max_bytes=self.sandbox.max_candidate_bytes,
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "optiprofiler_evolve.criteria.complexity._runner",
                    str(root.resolve()),
                ],
                capture_output=True,
                text=True,
                timeout=ANALYSIS_TIMEOUT,
                check=False,
                env={
                    "PATH": os.defpath,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Complexity analysis timed out.") from exc
        if completed.returncode:
            raise ValueError(
                "Complexity analysis failed; check Python syntax and analyzer availability."
            )
        payload = json.loads(completed.stdout)
        return ComplexityMetrics(
            payload["decision_complexity"], payload["logical_lines"], tuple(payload["hotspots"])
        )

    def measure(self, root: Path) -> ComplexityMetrics:
        digest = tree_hash(root)
        with self._lock:
            metrics = self._cache.get(digest)
        if metrics is None:
            metrics = self._analyze(root)
        if tree_hash(root) != digest:
            raise ValueError("Source changed during complexity analysis; submit again.")
        with self._lock:
            self._cache[digest] = metrics
        return metrics


def analyze_source(root: Path) -> ComplexityMetrics:
    """Use Radon's decision rules, traversing all lexical scopes exactly once."""
    import radon
    from radon.complexity import cc_visit_ast
    from radon.raw import analyze
    from radon.visitors import ComplexityVisitor

    if f"radon/{radon.__version__}" != ANALYZER:
        raise ValueError("The installed Radon version differs from the recorded metric contract.")

    class WholeSourceVisitor(ComplexityVisitor):
        visit_FunctionDef = ast.NodeVisitor.generic_visit
        visit_AsyncFunctionDef = ast.NodeVisitor.generic_visit
        visit_ClassDef = ast.NodeVisitor.generic_visit

    decisions = logical_lines = 0
    hotspots = []
    for path in source_files(root):
        if path.suffix != ".py":
            continue
        with tokenize.open(path) as handle:
            source = handle.read()
        tree = ast.parse(source)
        decisions += WholeSourceVisitor.from_ast(tree).complexity - 1
        docstrings = sum(
            ast.get_docstring(node, clean=False) is not None
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        )
        logical_lines += analyze(source).lloc - docstrings
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                block = cc_visit_ast(node)[0]
                hotspots.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "name": node.name,
                        "line": node.lineno,
                        "cc": block.complexity,
                    }
                )
    hotspots.sort(key=lambda item: (-item["cc"], item["path"], item["line"]))
    return ComplexityMetrics(1 + decisions, logical_lines, tuple(hotspots[:5]))
