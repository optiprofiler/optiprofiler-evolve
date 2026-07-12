"""Trusted OptiProfiler evaluation adapters."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import threading
import types
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from .config import EvaluationConfig
from .data import DataPlan
from .models import EvaluationResult
from .solver import InterfaceSpec, validate_interface


class Evaluator(Protocol):
    """Internal evaluation boundary used by the controller and tool broker."""

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult: ...


class PythonOptiProfilerEvaluator:
    """Run candidate and immutable reference together in OptiProfiler benchmark."""

    def __init__(
        self,
        *,
        reference: Path,
        interface: InterfaceSpec,
        data: DataPlan,
        config: EvaluationConfig,
    ) -> None:
        self.reference = reference.resolve()
        self.interface = interface
        self.data = data
        self.config = config
        self._lock = threading.RLock()

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        problems = _problems_for_mode(self.data, mode)
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        result_path = output_dir / "result.json"
        try:
            with self._lock:
                validate_interface(candidate, self.interface, "python")
                candidate_modules: set[str] = set()
                reference_modules: set[str] = set()
                try:
                    candidate_solver, candidate_modules = _load_python_solver(
                        candidate.resolve(), self.interface, "candidate"
                    )
                    reference_solver, reference_modules = _load_python_solver(
                        self.reference, self.interface, "reference"
                    )
                    result = self._benchmark(
                        candidate_solver, reference_solver, problems, mode, output_dir
                    )
                finally:
                    for name in candidate_modules.union(reference_modules):
                        sys.modules.pop(name, None)
        except Exception as exc:
            result = EvaluationResult(
                mode=mode,
                score=0.0,
                candidate_score=0.0,
                reference_score=0.0,
                problems=problems,
                output_dir=output_dir,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        result_path.write_text(
            json.dumps(_json_safe(result.as_dict()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_feedback(result, output_dir, self.config.feedback_mode)
        return result

    def _benchmark(
        self,
        candidate_solver: Any,
        reference_solver: Any,
        problems: tuple[str, ...],
        mode: str,
        output_dir: Path,
    ) -> EvaluationResult:
        try:
            from optiprofiler import benchmark
        except ImportError as exc:
            raise RuntimeError("optiprofiler is required for default evaluation.") from exc

        options = dict(self.config.benchmark)
        if mode == "smoke":
            options.update(self.config.smoke_overrides)
        protected = {
            "plibs": [self.data.library],
            "problem_names": list(problems),
            "solver_names": ["candidate", "initial"],
            "normalized_scores": True,
            "silent": True,
        }
        if self.data.custom_problem_libraries_path:
            protected["custom_problem_libs_path"] = self.data.custom_problem_libraries_path
        options.update(protected)
        if not options.get("score_only", False):
            options["savepath"] = str(output_dir / "benchmark")
            options["benchmark_id"] = mode

        scores, profile_scores, _curves = benchmark([candidate_solver, reference_solver], **options)
        if len(scores) != 2:
            raise RuntimeError(f"OptiProfiler returned {len(scores)} solver scores; expected 2.")
        candidate_score = float(scores[0])
        reference_score = float(scores[1])
        if not math.isfinite(candidate_score) or not math.isfinite(reference_score):
            raise RuntimeError("OptiProfiler returned a non-finite solver score.")
        normalized = min(1.0, max(0.0, (candidate_score - reference_score + 1.0) / 2.0))
        return EvaluationResult(
            mode=mode,
            score=normalized,
            candidate_score=candidate_score,
            reference_score=reference_score,
            problems=problems,
            output_dir=output_dir,
            profile_scores=_json_safe(profile_scores),
        )


class DockerOptiProfilerEvaluator:
    """Run the Python evaluator inside a separately hardened Docker container."""

    def __init__(
        self,
        *,
        reference: Path,
        interface: InterfaceSpec,
        data: DataPlan,
        config: EvaluationConfig,
    ) -> None:
        if not config.docker_image:
            raise ValueError("A Docker evaluation image is required.")
        self.reference = reference.resolve()
        self.interface = interface
        self.data = data
        self.config = config

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        candidate = candidate.resolve()
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        data_manifest = self.data.full_manifest()
        if self.data.custom_problem_libraries_path:
            data_manifest["custom_problem_libraries_path"] = "/problem-libraries"
        request = {
            "reference": "/reference",
            "candidate": "/candidate",
            "interface": f"{self.interface.file}:{self.interface.function}",
            "mode": mode,
            "output_dir": "/output",
            "data": data_manifest,
            "evaluation": asdict(self.config) | {"backend": "local"},
        }
        request_path = output_dir / "evaluation_request.json"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        container_name = f"ope-evaluator-{uuid.uuid4().hex[:12]}"
        command = self.command(candidate, output_dir, container_name)
        try:
            completed = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.config.timeout_seconds,
                check=False,
            )
            log = completed.stdout
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            log = exc.stdout or ""
            if isinstance(log, bytes):
                log = log.decode("utf-8", errors="replace")
            log += "\n[controller] evaluator timed out\n"
            returncode = 124
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        (output_dir / "evaluator.log").write_text(log, encoding="utf-8")
        result_path = output_dir / "result.json"
        if returncode != 0 or not result_path.is_file():
            error = f"Docker evaluator exited with code {returncode}."
            result = EvaluationResult(
                mode=mode,
                score=0.0,
                candidate_score=0.0,
                reference_score=0.0,
                problems=_problems_for_mode(self.data, mode),
                output_dir=output_dir,
                success=False,
                error=error,
            )
            result_path.write_text(json.dumps(result.as_dict(), indent=2) + "\n", encoding="utf-8")
            _write_feedback(result, output_dir, self.config.feedback_mode)
            return result
        result = _read_result(result_path)
        return EvaluationResult(**(result.as_dict() | {"output_dir": output_dir}))

    def command(self, candidate: Path, output_dir: Path, container_name: str) -> list[str]:
        command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--name",
            container_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--pids-limit",
            str(self.config.pids_limit),
            "--cpus",
            str(self.config.cpus),
            "--memory",
            self.config.memory,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=1g",
            "--env",
            "HOME=/tmp/home",
            "--env",
            "MPLCONFIGDIR=/tmp/matplotlib",
            "--mount",
            f"type=bind,src={candidate},dst=/candidate,readonly",
            "--mount",
            f"type=bind,src={self.reference},dst=/reference,readonly",
            "--mount",
            f"type=bind,src={output_dir},dst=/output",
        ]
        if self.data.custom_problem_libraries_path:
            custom = Path(self.data.custom_problem_libraries_path).expanduser().resolve()
            if not custom.is_dir():
                raise FileNotFoundError(f"Custom problem-library path does not exist: {custom}")
            command.extend(["--mount", f"type=bind,src={custom},dst=/problem-libraries,readonly"])
        command.extend(
            [
                self.config.docker_image or "",
                "python",
                "-m",
                "optiprofiler_evolve._evaluation_runner",
                "/output/evaluation_request.json",
            ]
        )
        return command


def create_evaluator(
    *,
    runtime: str,
    reference: Path,
    interface: InterfaceSpec,
    data: DataPlan,
    config: EvaluationConfig,
) -> Evaluator:
    """Create the trusted evaluator selected by entrypoint runtime and backend."""

    if runtime != "python":
        raise NotImplementedError(
            "The rewritten MVP currently has a complete Python OptiProfiler evaluator. "
            "MATLAB entrypoints are detected but require a future MATLAB adapter."
        )
    evaluator_type = (
        DockerOptiProfilerEvaluator if config.backend == "docker" else PythonOptiProfilerEvaluator
    )
    return evaluator_type(reference=reference, interface=interface, data=data, config=config)


def _problems_for_mode(data: DataPlan, mode: str) -> tuple[str, ...]:
    if mode == "smoke":
        return data.smoke
    if mode == "public":
        return data.public
    if mode == "final":
        return data.final
    raise ValueError(f"Unsupported evaluation mode: {mode!r}")


def _load_python_solver(root: Path, interface: InterfaceSpec, label: str) -> tuple[Any, set[str]]:
    unique = f"_optiprofiler_evolve_{label}_{uuid.uuid4().hex}"
    package = types.ModuleType(unique)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    package.__package__ = unique
    sys.modules[unique] = package
    created = {unique}

    parts = Path(interface.file).with_suffix("").parts
    parent_path = root
    parent_name = unique
    for part in parts[:-1]:
        parent_path /= part
        parent_name = f"{parent_name}.{part}"
        namespace = types.ModuleType(parent_name)
        namespace.__path__ = [str(parent_path)]  # type: ignore[attr-defined]
        namespace.__package__ = parent_name
        sys.modules[parent_name] = namespace
        created.add(parent_name)

    module_name = f"{parent_name}.{parts[-1]}"
    module_path = root / interface.file
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load solver interface from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    created.add(module_name)

    old_path = list(sys.path)
    before = set(sys.modules)
    try:
        sys.path.insert(0, str(root))
        spec.loader.exec_module(module)
    except Exception:
        for name in set(sys.modules).difference(before).union(created):
            loaded = sys.modules.get(name)
            file_name = getattr(loaded, "__file__", None)
            if name in created or (file_name and _is_inside(Path(file_name), root)):
                sys.modules.pop(name, None)
        raise
    finally:
        sys.path[:] = old_path
    for name in set(sys.modules).difference(before):
        loaded = sys.modules.get(name)
        file_name = getattr(loaded, "__file__", None)
        if file_name and _is_inside(Path(file_name), root):
            created.add(name)
    # Unqualified local imports would otherwise make the reference reuse the
    # candidate's module. Eager imports remain alive through solver globals;
    # solver repositories should use relative imports for lazy dependencies.
    for name in tuple(created):
        if not name.startswith(unique):
            sys.modules.pop(name, None)
    solver = getattr(module, interface.function, None)
    if not callable(solver):
        raise TypeError(f"Interface {interface.function!r} is not callable.")
    return solver, created


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _write_feedback(result: EvaluationResult, output_dir: Path, feedback_mode: str) -> None:
    if result.success:
        text = (
            f"# Evaluation: {result.mode}\n\n"
            f"- normalized fitness: `{result.score:.6f}`\n"
            f"- candidate OptiProfiler score: `{result.candidate_score:.6f}`\n"
            f"- immutable initial score: `{result.reference_score:.6f}`\n"
            f"- problems: `{len(result.problems)}`\n\n"
            "The normalized fitness is `(candidate - initial + 1) / 2`; "
            "`0.5` is a tie with the initial solver.\n"
        )
        if feedback_mode == "agent":
            numbers = list(_finite_numbers(result.profile_scores))
            text += "\n## Profile signal\n\n"
            if numbers:
                text += (
                    f"- profile values: `{len(numbers)}`\n"
                    f"- range: `[{min(numbers):.6g}, {max(numbers):.6g}]`\n"
                    f"- mean: `{sum(numbers) / len(numbers):.6g}`\n"
                )
            else:
                text += "No finite profile-level values were returned.\n"
            artifacts = [
                path.relative_to(output_dir).as_posix()
                for path in sorted(output_dir.rglob("*"))
                if path.is_file() and path.name not in {"result.json", "feedback.md"}
            ]
            if artifacts:
                text += "\n## Benchmark artifacts\n\n"
                text += "\n".join(f"- `{name}`" for name in artifacts[:80]) + "\n"
                if len(artifacts) > 80:
                    text += f"- ... and {len(artifacts) - 80} more files\n"
    else:
        text = f"# Evaluation failed\n\n`{result.error}`\n"
    (output_dir / "feedback.md").write_text(text, encoding="utf-8")


def _finite_numbers(value: Any):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            yield number
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _finite_numbers(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _finite_numbers(item)


def _read_result(path: Path) -> EvaluationResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["problems"] = tuple(raw["problems"])
    raw["output_dir"] = Path(raw["output_dir"])
    return EvaluationResult(**raw)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    return repr(value)


__all__: list[str] = []
