"""Trusted OptiProfiler evaluation adapters."""

from __future__ import annotations

import importlib.util
import json
import math
import mimetypes
import os
import stat
import subprocess
import sys
import tempfile
import threading
import types
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import EvaluationConfig, plain_data
from .data import DataPlan
from .models import EvaluationResult
from .protocols import Evaluator
from .solver import InterfaceSpec, validate_interface


class PythonOptiProfilerEvaluator:
    """Run candidate and immutable reference together in OptiProfiler benchmark."""

    name = "optiprofiler-python"
    deterministic = False

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
                problem_count=len(problems),
                output_dir=output_dir,
                success=False,
                error=_redact_problem_names(f"{type(exc).__name__}: {exc}", self.data),
            )
        if result.profile_scores is not None:
            (output_dir / "profile_scores.json").write_text(
                json.dumps(_json_safe(result.profile_scores), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        result_path.write_text(
            json.dumps(_json_safe(result.as_dict()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_feedback(result, output_dir, self.config.feedback_mode)
        _sanitize_worker_artifacts(output_dir, self.data)
        _write_artifact_index(output_dir)
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
        if mode in {"public_score", "validation"}:
            options["score_only"] = True
            options["draw_hist_plots"] = "none"
        with tempfile.TemporaryDirectory(prefix="ope-dataset-") as temporary:
            opaque_root = Path(temporary)
            opaque_library = _write_opaque_problem_library(self.data, problems, opaque_root)
            protected = {
                "plibs": [opaque_library],
                "problem_names": [self.data.opaque_name(name) for name in problems],
                "solver_names": ["candidate", "reference"],
                "normalized_scores": True,
                "silent": True,
                "custom_problem_libs_path": str(opaque_root),
            }
            options.update(protected)
            if not options.get("score_only", False):
                options["savepath"] = str(output_dir / "benchmark")
                options["benchmark_id"] = mode

            scores, profile_scores, _curves = benchmark(
                [candidate_solver, reference_solver], **options
            )
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
            problem_count=len(problems),
            output_dir=output_dir,
            profile_scores=_json_safe(profile_scores),
        )


class DockerOptiProfilerEvaluator:
    """Run the Python evaluator inside a separately hardened Docker container."""

    name = "optiprofiler-docker"
    deterministic = False

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
        data_manifest = _scoped_data_manifest(self.data, mode)
        if self.data.custom_problem_libraries_path:
            data_manifest["custom_problem_libraries_path"] = "/problem-libraries"
        request = {
            "reference": "/reference",
            "candidate": "/candidate",
            "interface": f"{self.interface.file}:{self.interface.function}",
            "mode": mode,
            "output_dir": "/output",
            "data": data_manifest,
            "evaluation": plain_data(self.config) | {"backend": "unsafe_local"},
        }
        container_name = f"ope-evaluator-{uuid.uuid4().hex[:12]}"
        # The request directory is a Docker bind source, so it must live under
        # a run-owned path: output_dir.parent is controller-private at every
        # call site and shared into Docker VMs, unlike the macOS system
        # tempdir (/var/folders is invisible to Colima and the mount fails).
        # As a sibling of output_dir it is never published with the results.
        with tempfile.TemporaryDirectory(
            prefix=".ope-evaluator-request-",
            dir=output_dir.parent,
        ) as request_dir:
            request_path = Path(request_dir) / "evaluation_request.json"
            request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
            command = self.command(candidate, output_dir, container_name, request_path)
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
        log = _redact_problem_names(log, self.data)
        (output_dir / "evaluator.log").write_text(log, encoding="utf-8")
        result_path = output_dir / "result.json"
        if returncode != 0 or not result_path.is_file():
            error = f"Docker evaluator exited with code {returncode}."
            result = EvaluationResult(
                mode=mode,
                score=0.0,
                candidate_score=0.0,
                reference_score=0.0,
                problem_count=len(_problems_for_mode(self.data, mode)),
                output_dir=output_dir,
                success=False,
                error=error,
            )
            result_path.write_text(json.dumps(result.as_dict(), indent=2) + "\n", encoding="utf-8")
            _write_feedback(result, output_dir, self.config.feedback_mode)
            _sanitize_worker_artifacts(output_dir, self.data)
            _write_artifact_index(output_dir)
            return result
        result = _read_result(result_path)
        result = replace(
            result,
            output_dir=output_dir,
            error=_redact_problem_names(result.error, self.data),
        )
        result_path.write_text(
            json.dumps(_json_safe(result.as_dict()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _sanitize_worker_artifacts(output_dir, self.data)
        _write_artifact_index(output_dir)
        return result

    def command(
        self,
        candidate: Path,
        output_dir: Path,
        container_name: str,
        request_path: Path,
    ) -> list[str]:
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
            "--tmpfs",
            "/pycutest-cache:rw,exec,nosuid,nodev,size=2g",
            "--env",
            "HOME=/tmp/home",
            "--env",
            "MPLCONFIGDIR=/tmp/matplotlib",
            "--env",
            "PYCUTEST_CACHE=/pycutest-cache",
            "--env",
            "OMP_NUM_THREADS=1",
            "--env",
            "OPENBLAS_NUM_THREADS=1",
            "--env",
            "MKL_NUM_THREADS=1",
            "--env",
            "NUMEXPR_NUM_THREADS=1",
            "--mount",
            f"type=bind,src={candidate},dst=/candidate,readonly",
            "--mount",
            f"type=bind,src={self.reference},dst=/reference,readonly",
            "--mount",
            f"type=bind,src={output_dir},dst=/output",
            "--mount",
            # Deliberately writable: the in-container runner unlinks the
            # request (it carries real problem names) before candidate code
            # can run, and a readonly bind would break that scrubbing.
            f"type=bind,src={request_path.parent},dst=/request",
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
                "/request/evaluation_request.json",
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


setattr(create_evaluator, "deterministic", False)


def _problems_for_mode(data: DataPlan, mode: str) -> tuple[str, ...]:
    if mode == "smoke":
        return data.smoke
    if mode in {"public", "public_score"}:
        return data.public
    if mode == "validation":
        return data.selection_problems
    if mode == "hidden":
        return data.hidden
    raise ValueError(f"Unsupported evaluation mode: {mode!r}")


def _scoped_data_manifest(data: DataPlan, mode: str) -> dict[str, Any]:
    """Give an evaluator only the names required by its current capability."""

    problems = _problems_for_mode(data, mode)
    aliases = {name: data.opaque_name(name) for name in problems}
    return {
        "library": data.library,
        "selection": {},
        "universe": list(problems),
        "public": list(problems),
        "validation": list(problems) if mode == "validation" else [],
        "hidden": list(problems) if mode == "hidden" else [],
        "smoke": list(problems) if mode == "smoke" else [],
        "split_seed": data.split_seed,
        "manifest_hash": data.manifest_hash,
        "custom_problem_libraries_path": data.custom_problem_libraries_path,
        "aliases": aliases,
    }


def _write_opaque_problem_library(
    data: DataPlan,
    problems: tuple[str, ...],
    root: Path,
) -> str:
    """Create a temporary proxy so every benchmark artifact uses opaque IDs."""

    library_name = "ope_dataset"
    library_dir = root / library_name
    library_dir.mkdir(parents=True)
    mapping = {data.opaque_name(name): name for name in problems}
    source = f'''"""Ephemeral trusted problem-name proxy."""

import importlib

try:
    from optiprofiler.problem_libraries import (
        _resolve_problem_library_options,
        load_problem_library,
        resolve_problem_library,
    )
except ImportError:
    _HAS_PLUGIN_PROTOCOL = False
else:
    _HAS_PLUGIN_PROTOCOL = True

_MAPPING = {mapping!r}
_SOURCE_LIBRARY = {data.library!r}
_SOURCE_CUSTOM_PATH = {data.custom_problem_libraries_path!r}
_PLUGIN = None
_OPTIONS = None


def _source_plugin():
    global _PLUGIN, _OPTIONS
    if _PLUGIN is None:
        if _HAS_PLUGIN_PROTOCOL:
            reference = resolve_problem_library(_SOURCE_LIBRARY, _SOURCE_CUSTOM_PATH)
            _PLUGIN = load_problem_library(reference)
            _OPTIONS = _resolve_problem_library_options(_PLUGIN, {{}})
        elif _SOURCE_CUSTOM_PATH is None:
            _PLUGIN = importlib.import_module(
                f"optiprofiler.problem_libs.{{_SOURCE_LIBRARY}}"
            )
    return _PLUGIN, _OPTIONS


def {library_name}_select(_options):
    return list(_MAPPING)


def {library_name}_load(problem_name):
    plugin, options = _source_plugin()
    source_name = _MAPPING[problem_name]
    if _HAS_PLUGIN_PROTOCOL:
        return plugin.load(source_name, options)
    if plugin is None:
        raise RuntimeError(
            "Custom problem libraries require the current OptiProfiler plugin protocol."
        )
    return getattr(plugin, f"{{_SOURCE_LIBRARY}}_load")(source_name)
'''
    (library_dir / f"{library_name}_tools.py").write_text(source, encoding="utf-8")
    return library_name


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
            f"- fitness: `{result.score:.6f}`\n"
            "- direction: `maximize`\n"
            f"- problems: `{result.problem_count}`\n\n"
            "Use this fitness only to compare candidates produced within this run.\n"
        )
        if feedback_mode == "agent":
            text += (
                "\n## Benchmark artifacts\n\n"
                "Read `artifact_index.json` to discover the complete benchmark bundle. "
                "Problem identifiers in every worker-visible file are opaque and stable "
                "within this run. `profile_scores.json` contains the structured "
                "profile-level output when available.\n"
            )
    else:
        text = f"# Evaluation failed\n\n`{result.error}`\n"
    (output_dir / "feedback.md").write_text(text, encoding="utf-8")


def _write_artifact_index(output_dir: Path) -> None:
    entries = []
    excluded = {"artifact_index.json", "feedback.md", "result.json"}
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name in excluded:
            continue
        media_type, _encoding = mimetypes.guess_type(path.name)
        entries.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "bytes": path.stat().st_size,
                "media_type": media_type or "application/octet-stream",
            }
        )
    (output_dir / "artifact_index.json").write_text(
        json.dumps({"artifacts": entries}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_result(path: Path) -> EvaluationResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
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


def _redact_problem_names(value: str | None, data: DataPlan) -> str | None:
    if value is None:
        return None
    redacted = value
    for problem_name in sorted(data.universe, key=len, reverse=True):
        redacted = redacted.replace(problem_name, data.opaque_name(problem_name))
    return redacted


def _sanitize_worker_artifacts(output_dir: Path, data: DataPlan) -> None:
    """Remove direct problem-name disclosures before artifacts reach a worker.

    Text is rewritten with the run's opaque identifiers. A binary file that
    contains a literal protected name is removed because byte replacement could
    corrupt its format. This closes accidental and direct-string disclosure; it
    is not a substitute for the stronger candidate-process boundary documented
    in ``docs/security.md``.
    """

    replacements = {
        name: data.opaque_name(name) for name in data.universe if data.opaque_name(name) != name
    }
    if not replacements or not output_dir.is_dir():
        return
    removed_binary = 0
    for original in list(output_dir.rglob("*")):
        metadata = original.lstat()
        if original.is_symlink():
            original.unlink()
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        path = original
        relative = path.relative_to(output_dir).as_posix()
        redacted_relative = relative
        for name in sorted(replacements, key=len, reverse=True):
            redacted_relative = redacted_relative.replace(name, replacements[name])
        if redacted_relative != relative:
            target = output_dir / redacted_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                path.unlink()
                continue
            path.replace(target)
            path = target

        content = path.read_bytes()
        protected = [name for name in replacements if name.encode("utf-8") in content]
        if not protected:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            path.unlink()
            removed_binary += 1
            continue
        for name in sorted(protected, key=len, reverse=True):
            text = text.replace(name, replacements[name])
        path.write_text(text, encoding="utf-8")

    for directory in sorted(
        (
            path
            for path in output_dir.rglob("*")
            if not path.is_symlink() and stat.S_ISDIR(path.lstat().st_mode)
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    if removed_binary:
        (output_dir / "redaction_report.json").write_text(
            json.dumps({"removed_binary_artifacts": removed_binary}, indent=2) + "\n",
            encoding="utf-8",
        )


__all__: list[str] = []
