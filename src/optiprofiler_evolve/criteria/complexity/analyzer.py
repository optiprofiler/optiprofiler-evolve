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

from optiprofiler_evolve.config import ComplexityConfig, SandboxConfig
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


@dataclass(frozen=True)
class ComplexityReport:
    tree_hash: str
    baseline_hash: str
    baseline: ComplexityMetrics
    candidate: ComplexityMetrics | None
    mode: str
    max_ratio: float
    error: str | None = None
    analyzer: str = ANALYZER
    schema: str = METRICS_VERSION

    @property
    def ratios(self) -> tuple[float, float] | None:
        if self.candidate is None:
            return None
        return (
            self.candidate.decision_complexity / self.baseline.decision_complexity,
            self.candidate.logical_lines / max(self.baseline.logical_lines, 1),
        )

    @property
    def score(self) -> float | None:
        ratios = self.ratios
        return 1 / (1 + max(ratios)) if ratios is not None else None

    @property
    def status(self) -> str:
        if self.ratios is None:
            return "unavailable"
        return "within_limit" if max(self.ratios) <= self.max_ratio else "exceeded"

    @property
    def blocked(self) -> bool:
        return self.mode == "enforce" and self.status != "within_limit"

    def summary(self) -> dict[str, object]:
        ratios = self.ratios
        return {
            "mode": self.mode,
            "status": self.status,
            "max_ratio": self.max_ratio,
            "branch_ratio": ratios[0] if ratios else None,
            "size_ratio": ratios[1] if ratios else None,
            "complexity_ratio": max(ratios) if ratios else None,
            "score": self.score,
            "complexity_percent": 100 * max(ratios) if ratios else None,
            "blocked": self.blocked,
        }

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), **self.summary()}

    def worker_dict(self) -> dict[str, object]:
        # Initial source locations and hashes remain controller-private.
        return {
            **self.summary(),
            "schema": self.schema,
            "analyzer": self.analyzer,
            "baseline": {
                "decision_complexity": self.baseline.decision_complexity,
                "logical_lines": self.baseline.logical_lines,
            },
            "candidate": asdict(self.candidate) if self.candidate else None,
            "error": self.error,
        }


class ComplexityChecker:
    """Cache successful analyses; each instance owns one fixed baseline and policy."""

    def __init__(
        self, seed: Path, config: ComplexityConfig, sandbox: SandboxConfig, output: Path
    ) -> None:
        self.config = config
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

    def assess(self, root: Path) -> ComplexityReport:
        digest = tree_hash(root)
        error = None
        with self._lock:
            metrics = self._cache.get(digest)
        try:
            if metrics is None:
                metrics = self._analyze(root)
            if tree_hash(root) != digest:
                raise ValueError("Source changed during complexity analysis; submit again.")
        except (ValueError, OSError) as exc:
            metrics = None
            error = str(exc)
        report = ComplexityReport(
            digest, self.baseline_hash, self.baseline, metrics,
            self.config.mode, self.config.max_ratio, error,
        )
        # Only shared cache entries and same-hash report writes require serialization.
        with self._lock:
            if metrics is not None:
                self._cache[digest] = metrics
            write_json(self.output / f"{digest}.json", report.as_dict())
        return report


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
