"""Strict configuration for an OptiProfiler Evolve experiment."""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

import yaml


_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_T = TypeVar("_T")


@dataclasses.dataclass(frozen=True)
class SplitConfig:
    """Deterministic public/hidden/smoke split settings."""

    hidden_fraction: float = 0.2
    smoke_count: int = 3
    seed: int = 0
    public: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()

    def validate(self) -> None:
        if not 0 <= self.hidden_fraction < 1:
            raise ValueError("data.split.hidden_fraction must be in [0, 1).")
        if self.smoke_count < 1:
            raise ValueError("data.split.smoke_count must be positive.")
        if bool(self.public) != bool(self.hidden):
            raise ValueError("Explicit data.split.public and hidden must be provided together.")
        overlap = set(self.public).intersection(self.hidden)
        if overlap:
            raise ValueError(f"Explicit public and hidden sets overlap: {sorted(overlap)!r}")


@dataclasses.dataclass(frozen=True)
class DataConfig:
    """Problem-library selection and split settings."""

    library: str = "s2mpj"
    selection: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    problem_names: tuple[str, ...] = ()
    custom_problem_libraries_path: str | None = None
    split: SplitConfig = dataclasses.field(default_factory=SplitConfig)

    def validate(self) -> None:
        if not self.library or not self.library.isidentifier():
            raise ValueError("data.library must be a non-empty Python identifier.")
        if len(set(self.problem_names)) != len(self.problem_names):
            raise ValueError("data.problem_names contains duplicates.")
        self.split.validate()


@dataclasses.dataclass(frozen=True)
class EvaluationConfig:
    """OptiProfiler benchmark execution settings."""

    benchmark: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    smoke_overrides: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    backend: str = "docker"
    docker_image: str | None = None
    timeout_seconds: int = 3600
    cpus: float = 4.0
    memory: str = "8g"
    pids_limit: int = 512
    feedback_mode: str = "summary"
    max_smoke_calls_per_worker: int = 20
    max_public_calls_per_worker: int = 5

    def validate(self) -> None:
        if self.backend not in {"docker", "local"}:
            raise ValueError("evaluation.backend must be 'docker' or 'local'.")
        if self.backend == "docker" and not self.docker_image:
            raise ValueError("evaluation.docker_image is required for Docker evaluation.")
        if self.timeout_seconds < 1:
            raise ValueError("evaluation.timeout_seconds must be positive.")
        if self.cpus <= 0 or self.pids_limit < 16 or not self.memory:
            raise ValueError(
                "evaluation cpus/memory must be set and pids_limit must be at least 16."
            )
        if self.feedback_mode not in {"summary", "agent"}:
            raise ValueError("evaluation.feedback_mode must be 'summary' or 'agent'.")
        if self.max_smoke_calls_per_worker < 0 or self.max_public_calls_per_worker < 0:
            raise ValueError("Evaluation call quotas cannot be negative.")
        forbidden = {"load", "solvers_to_load"}
        configured = forbidden.intersection(self.benchmark) | forbidden.intersection(
            self.smoke_overrides
        )
        if configured:
            raise ValueError(
                f"Evaluation cannot load previous benchmark results: {sorted(configured)!r}"
            )


@dataclasses.dataclass(frozen=True)
class EvolutionConfig:
    """Population scheduling settings."""

    rounds: int = 3
    islands: int = 4
    population_per_island: int = 4
    migration_interval: int = 2
    random_seed: int = 0
    finalists_per_island: int = 1

    def validate(self) -> None:
        for name in ("rounds", "islands", "population_per_island", "finalists_per_island"):
            if getattr(self, name) < 1:
                raise ValueError(f"evolution.{name} must be positive.")
        if self.migration_interval < 0:
            raise ValueError("evolution.migration_interval cannot be negative.")
        if self.finalists_per_island > self.population_per_island:
            raise ValueError("finalists_per_island cannot exceed population_per_island.")


@dataclasses.dataclass(frozen=True)
class WorkerConfig:
    """One coding-agent worker choice in the scheduling pool."""

    harness: str
    model: str
    weight: int = 1
    profile: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    pass_env: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.harness not in {"codex", "claude"}:
            raise ValueError("worker harness must be 'codex' or 'claude'.")
        if not self.model:
            raise ValueError("worker model cannot be empty.")
        if self.weight < 1:
            raise ValueError("worker weight must be positive.")
        for key in (*self.env.keys(), *self.pass_env):
            if not key or not key.replace("_", "A").isalnum():
                raise ValueError(f"Invalid worker environment variable name: {key!r}")


@dataclasses.dataclass(frozen=True)
class ToolConfig:
    """Tools exposed inside a worker sandbox."""

    preset: str = "research"
    web_search: bool = True
    network: bool = True
    shell: bool = True
    python: bool = True
    git: bool = True
    compilers: bool = True
    package_install: bool = False
    communication: str = "none"

    def validate(self) -> None:
        if self.preset not in {"minimal", "research", "custom"}:
            raise ValueError("tools.preset must be minimal, research, or custom.")
        if self.communication not in {"none", "controller_summary", "island", "global"}:
            raise ValueError("Unsupported tools.communication mode.")
        if self.web_search and not self.network:
            raise ValueError("tools.web_search requires tools.network.")


@dataclasses.dataclass(frozen=True)
class WorkersConfig:
    """Worker pool and common budgets."""

    pool: tuple[WorkerConfig, ...] = ()
    max_parallel: int = 4
    timeout_seconds: int = 1800
    token_budget: int | None = None
    max_budget_usd: float | None = None
    tools: ToolConfig = dataclasses.field(default_factory=ToolConfig)

    def validate(self) -> None:
        if not self.pool:
            raise ValueError("workers.pool must contain at least one worker.")
        if self.max_parallel < 1 or self.timeout_seconds < 1:
            raise ValueError("workers max_parallel and timeout_seconds must be positive.")
        if self.token_budget is not None and self.token_budget < 1:
            raise ValueError("workers.token_budget must be positive when set.")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise ValueError("workers.max_budget_usd must be positive when set.")
        for worker in self.pool:
            worker.validate()
        self.tools.validate()


@dataclasses.dataclass(frozen=True)
class SandboxConfig:
    """Coding-agent sandbox settings."""

    backend: str = "docker"
    worker_image: str = "optiprofiler-evolve-worker:latest"
    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512
    max_candidate_files: int = 2000
    max_candidate_bytes: int = 200_000_000

    def validate(self) -> None:
        if self.backend not in {"docker", "unsafe_local"}:
            raise ValueError("sandbox.backend must be 'docker' or 'unsafe_local'.")
        if self.cpus <= 0 or self.pids_limit < 16 or not self.memory:
            raise ValueError("sandbox cpus/memory must be set and pids_limit must be at least 16.")
        if self.max_candidate_files < 1 or self.max_candidate_bytes < 1:
            raise ValueError("sandbox candidate file and byte limits must be positive.")


@dataclasses.dataclass(frozen=True)
class EvolveConfig:
    """Complete experiment configuration."""

    data: DataConfig = dataclasses.field(default_factory=DataConfig)
    evaluation: EvaluationConfig = dataclasses.field(default_factory=EvaluationConfig)
    evolution: EvolutionConfig = dataclasses.field(default_factory=EvolutionConfig)
    workers: WorkersConfig = dataclasses.field(default_factory=WorkersConfig)
    sandbox: SandboxConfig = dataclasses.field(default_factory=SandboxConfig)

    def validate(self) -> None:
        self.data.validate()
        self.evaluation.validate()
        self.evolution.validate()
        self.workers.validate()
        self.sandbox.validate()

    def redacted_dict(self) -> dict[str, Any]:
        """Return a serializable config without credential values."""

        result = dataclasses.asdict(self)
        for worker in result["workers"]["pool"]:
            worker["env"] = {
                key: "<redacted>" if _is_secret_name(key) else value
                for key, value in worker["env"].items()
            }
        return result


def load_config(config: EvolveConfig | Mapping[str, Any] | str | Path) -> EvolveConfig:
    """Load, expand environment references, and validate a strict config."""

    if isinstance(config, EvolveConfig):
        loaded = config
    else:
        if isinstance(config, (str, Path)):
            path = Path(config).expanduser().resolve()
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif isinstance(config, Mapping):
            raw = dict(config)
        else:
            raise TypeError("config must be an EvolveConfig, mapping, or YAML path.")
        if not isinstance(raw, Mapping):
            raise TypeError("The experiment config must contain a mapping at its root.")
        loaded = _from_mapping(EvolveConfig, _expand_environment(dict(raw)), "config")
    loaded.validate()
    return loaded


def _expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value)
        if match:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"Required environment variable {name!r} is not set.")
            return os.environ[name]
        return value
    if isinstance(value, Mapping):
        return {str(key): _expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    return value


def _from_mapping(cls: type[_T], raw: Mapping[str, Any], path: str) -> _T:
    fields = {field.name: field for field in dataclasses.fields(cls)}
    unknown = sorted(set(raw).difference(fields))
    if unknown:
        raise ValueError(f"Unknown keys at {path}: {unknown!r}")

    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        annotation = hints[name]
        nested = _nested_dataclass(annotation)
        if nested is not None:
            if not isinstance(value, Mapping):
                raise TypeError(f"{path}.{name} must be a mapping.")
            kwargs[name] = _from_mapping(nested, value, f"{path}.{name}")
            continue
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is tuple and args and dataclasses.is_dataclass(args[0]):
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise TypeError(f"{path}.{name} must be a list.")
            kwargs[name] = tuple(
                _from_mapping(args[0], item, f"{path}.{name}[{index}]")
                for index, item in enumerate(value)
            )
        elif origin is tuple:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise TypeError(f"{path}.{name} must be a list.")
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def _nested_dataclass(annotation: Any) -> type[Any] | None:
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return annotation
    return None


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(
        marker in upper
        for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    )
