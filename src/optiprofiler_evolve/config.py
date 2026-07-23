"""Strict configuration for an OptiProfiler Evolve experiment."""

from __future__ import annotations

import dataclasses
import os
import re
import types
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Generic, TypeVar, Union, get_args, get_origin, get_type_hints
from urllib.parse import unquote, urlsplit

import yaml


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_T = TypeVar("_T")
_K = TypeVar("_K")
_V = TypeVar("_V")


class FrozenDict(Mapping[_K, _V], Generic[_K, _V]):
    """A small immutable mapping used inside frozen experiment configs."""

    def __init__(self, value: Mapping[_K, _V] | None = None) -> None:
        self._data = dict(value or {})

    def __getitem__(self, key: _K) -> _V:
        return self._data[key]

    def __iter__(self) -> Iterator[_K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __deepcopy__(self, _memo: dict[int, object]) -> FrozenDict[_K, _V]:
        return self

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"


@dataclasses.dataclass(frozen=True)
class ComponentConfig:
    """A registered component reference or a trusted in-process component."""

    name: str
    options: Mapping[str, Any] = dataclasses.field(default_factory=FrozenDict)
    _factory: Any = dataclasses.field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("-", "_").isidentifier():
            raise ValueError(f"Invalid component name: {self.name!r}")
        object.__setattr__(self, "options", _freeze_value(self.options))

    @classmethod
    def from_component(cls, component: object, *, name: str | None = None) -> ComponentConfig:
        """Wrap trusted owner code without requiring registry modification."""

        resolved_name = name or getattr(component, "name", component.__class__.__name__)
        return cls(str(resolved_name), _factory=component)


def _default_phases() -> tuple[ComponentConfig, ...]:
    return tuple(
        ComponentConfig(name) for name in ("prepare", "explore", "validate", "hidden", "report")
    )


def _default_attempt_steps() -> tuple[ComponentConfig, ...]:
    return tuple(
        ComponentConfig(name)
        for name in ("mutate", "static_audit", "smoke", "public_evaluate", "feedback")
    )


def _default_after_iteration() -> tuple[ComponentConfig, ...]:
    return (ComponentConfig("migration"),)


def _default_retention() -> ComponentConfig:
    return ComponentConfig("validation_lexicographic")


def _default_parent_sampler() -> ComponentConfig:
    return ComponentConfig(
        "top_biased_validation_weighted",
        options={"greedy_ratio": 0.7},
    )


def _default_integrity_reviewer() -> ComponentConfig:
    return ComponentConfig("agent_integrity")


@dataclasses.dataclass(frozen=True)
class WorkflowConfig:
    """Ordered, bounded extension slots; deliberately not a DAG language."""

    phases: tuple[ComponentConfig, ...] = dataclasses.field(default_factory=_default_phases)
    attempt_steps: tuple[ComponentConfig, ...] = dataclasses.field(
        default_factory=_default_attempt_steps
    )
    after_iteration: tuple[ComponentConfig, ...] = dataclasses.field(
        default_factory=_default_after_iteration
    )

    def validate(self) -> None:
        _validate_unique_components(self.phases, "workflow.phases")
        _validate_unique_components(self.attempt_steps, "workflow.attempt_steps")
        _validate_unique_components(self.after_iteration, "workflow.after_iteration")
        phase_names = [item.name for item in self.phases]
        for required in ("prepare", "explore", "validate", "hidden", "report"):
            if required not in phase_names:
                raise ValueError(f"workflow.phases must include the core phase {required!r}.")
        positions = {name: phase_names.index(name) for name in phase_names}
        core_order = [positions[name] for name in ("prepare", "explore", "validate", "hidden", "report")]
        if core_order != sorted(core_order):
            raise ValueError(
                "Core workflow phases must stay ordered as prepare, explore, validate, hidden, report."
            )
        if "direction_scout" in positions and positions["direction_scout"] > positions["explore"]:
            raise ValueError("direction_scout must run before explore.")
        if "strategy_analysis" in positions and not (
            positions["explore"] < positions["strategy_analysis"] < positions["validate"]
        ):
            raise ValueError("strategy_analysis must run between explore and validate.")
        if "recombine" in positions:
            if "strategy_analysis" not in positions:
                raise ValueError("recombine requires strategy_analysis.")
            if not positions["strategy_analysis"] < positions["recombine"] < positions["validate"]:
                raise ValueError("recombine must run after strategy_analysis and before validate.")
        if "challenger" in positions and not (
            positions["hidden"] < positions["challenger"] < positions["report"]
        ):
            raise ValueError("challenger must run after hidden and before report.")
        step_names = {item.name for item in self.attempt_steps}
        for required in ("mutate", "public_evaluate"):
            if required not in step_names:
                raise ValueError(f"workflow.attempt_steps must include {required!r}.")


@dataclasses.dataclass(frozen=True)
class SplitConfig:
    """Deterministic public/validation/hidden/smoke split settings."""

    validation_fraction: float = 0.2
    hidden_fraction: float = 0.2
    smoke_count: int = 3
    seed: int = 0
    public: tuple[str, ...] = ()
    validation: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()
    smoke: tuple[str, ...] = ()

    def validate(self) -> None:
        if not 0 <= self.validation_fraction < 1:
            raise ValueError("data.split.validation_fraction must be in [0, 1).")
        if not 0 <= self.hidden_fraction < 1:
            raise ValueError("data.split.hidden_fraction must be in [0, 1).")
        if self.validation_fraction + self.hidden_fraction >= 1:
            raise ValueError(
                "data.split.validation_fraction + hidden_fraction must be less than 1."
            )
        if self.smoke_count < 1:
            raise ValueError("data.split.smoke_count must be positive.")
        explicit = bool(self.public) or bool(self.validation) or bool(self.hidden)
        if explicit and not self.public:
            raise ValueError("An explicit data split must provide a nonempty public set.")
        groups = {
            "public": set(self.public),
            "validation": set(self.validation),
            "hidden": set(self.hidden),
        }
        for left, right in (
            ("public", "validation"),
            ("public", "hidden"),
            ("validation", "hidden"),
        ):
            overlap = groups[left].intersection(groups[right])
            if overlap:
                raise ValueError(f"Explicit {left} and {right} sets overlap: {sorted(overlap)!r}")
        if self.smoke and not set(self.smoke).issubset(groups["public"]):
            raise ValueError("Explicit data.split.smoke must be a subset of public.")


@dataclasses.dataclass(frozen=True)
class DataConfig:
    """Problem-library selection and split settings."""

    library: str = "s2mpj"
    selection: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    problem_names: tuple[str, ...] = ()
    custom_problem_libraries_path: str | None = None
    split: SplitConfig = dataclasses.field(default_factory=SplitConfig)

    def __post_init__(self) -> None:
        object.__setattr__(self, "selection", _freeze_value(self.selection))

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
    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512
    feedback_mode: str = "summary"
    reference: str = "initial"
    forbidden_candidate_imports: tuple[str, ...] = ()
    max_smoke_calls_per_worker: int = 20
    max_public_calls_per_worker: int = 5
    adapter: str = "optiprofiler"

    def __post_init__(self) -> None:
        object.__setattr__(self, "benchmark", _freeze_value(self.benchmark))
        object.__setattr__(self, "smoke_overrides", _freeze_value(self.smoke_overrides))

    def validate(self) -> None:
        if self.backend not in {"docker", "unsafe_local"}:
            raise ValueError("evaluation.backend must be 'docker' or 'unsafe_local'.")
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
        if not self.adapter or not self.adapter.replace("-", "_").isidentifier():
            raise ValueError("evaluation.adapter must be a registered component name.")
        if self.reference not in {"initial", "scipy_powell", "prima_newuoa"}:
            raise ValueError(
                "evaluation.reference must be 'initial', 'scipy_powell', "
                "or 'prima_newuoa'."
            )
        for name in self.forbidden_candidate_imports:
            if not name or any(not part.isidentifier() for part in name.split(".")):
                raise ValueError(
                    "evaluation.forbidden_candidate_imports must contain dotted "
                    "Python module names."
                )
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

    iterations: int = 3
    islands: int = 4
    attempts_per_island: int = 1
    population_per_island: int = 4
    migration_interval: int = 2
    random_seed: int = 0
    finalists_per_island: int = 1
    retention: ComponentConfig = dataclasses.field(default_factory=_default_retention)
    parent_sampler: ComponentConfig = dataclasses.field(default_factory=_default_parent_sampler)

    @property
    def rounds(self) -> int:
        """Compatibility alias for pre-constitution internal callers."""

        return self.iterations

    def validate(self) -> None:
        for name in (
            "iterations",
            "islands",
            "attempts_per_island",
            "population_per_island",
            "finalists_per_island",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"evolution.{name} must be positive.")
        if self.migration_interval < 0:
            raise ValueError("evolution.migration_interval cannot be negative.")
        if self.finalists_per_island > self.population_per_island:
            raise ValueError("finalists_per_island cannot exceed population_per_island.")


@dataclasses.dataclass(frozen=True)
class ProviderGatewayConfig:
    """Pinned provider route whose credential stays outside the worker."""

    upstream_base_url: str
    credential_env: str
    protocol: str = "auto"
    auth_mode: str = "auto"
    max_request_bytes: int = 16_000_000
    connect_timeout_seconds: int = 15
    response_timeout_seconds: int = 900

    def validate(self, harness: str) -> None:
        expected = "anthropic" if harness == "claude" else "openai_responses"
        if self.protocol not in {"auto", expected}:
            raise ValueError(
                f"provider_gateway.protocol for {harness!r} must be 'auto' or "
                f"{expected!r}."
            )
        if self.auth_mode not in {"auto", "bearer", "x-api-key"}:
            raise ValueError(
                "provider_gateway.auth_mode must be auto, bearer, or x-api-key."
            )
        if not self.credential_env or not self.credential_env.replace("_", "A").isalnum():
            raise ValueError("provider_gateway.credential_env must be an environment name.")
        parsed = urlsplit(self.upstream_base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(
                "provider_gateway.upstream_base_url must be an https origin or base URL."
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "provider_gateway.upstream_base_url cannot contain credentials, query, or fragment."
            )
        decoded_path = unquote(parsed.path)
        if any(part in {".", ".."} for part in decoded_path.split("/")):
            raise ValueError("provider_gateway.upstream_base_url contains path traversal.")
        if self.max_request_bytes < 1:
            raise ValueError("provider_gateway.max_request_bytes must be positive.")
        if self.connect_timeout_seconds < 1 or self.response_timeout_seconds < 1:
            raise ValueError("provider_gateway timeouts must be positive.")

    def resolved_protocol(self, harness: str) -> str:
        """Return the fixed wire protocol for one supported harness."""

        return (
            "anthropic" if harness == "claude" else "openai_responses"
        ) if self.protocol == "auto" else self.protocol


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
    provider_gateway: ProviderGatewayConfig | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", _freeze_value(self.env))

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
        gateway = self.provider_gateway
        if gateway is not None:
            gateway.validate(self.harness)
            if gateway.credential_env not in {*self.env, *self.pass_env}:
                raise ValueError(
                    "provider_gateway.credential_env must be supplied by worker env or pass_env."
                )
            if self.harness == "codex":
                if self.profile is not None:
                    raise ValueError(
                        "A gateway-routed Codex worker cannot load a provider profile."
                    )
                if any(
                    "model_provider" in argument or "model_providers." in argument
                    for argument in self.args
                ):
                    raise ValueError(
                        "Codex provider routing is controller-owned when provider_gateway is set."
                    )
            controlled = (
                {"ANTHROPIC_BASE_URL"}
                if self.harness == "claude"
                else {"OPENAI_BASE_URL"}
            )
            overlap = controlled.intersection(self.env)
            if overlap:
                raise ValueError(
                    "Provider base URLs are controller-owned when provider_gateway is set: "
                    f"{sorted(overlap)!r}"
                )


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
    adapter: str = "cli"
    allow_direct_provider: bool = False

    def validate(self) -> None:
        if not self.pool:
            raise ValueError("workers.pool must contain at least one worker.")
        if self.max_parallel < 1 or self.timeout_seconds < 1:
            raise ValueError("workers max_parallel and timeout_seconds must be positive.")
        if self.token_budget is not None and self.token_budget < 1:
            raise ValueError("workers.token_budget must be positive when set.")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise ValueError("workers.max_budget_usd must be positive when set.")
        if not self.adapter or not self.adapter.replace("-", "_").isidentifier():
            raise ValueError("workers.adapter must be a registered component name.")
        for worker in self.pool:
            worker.validate()
        self.tools.validate()
        if (
            self.adapter == "cli"
            and not self.tools.shell
            and any(worker.harness == "codex" for worker in self.pool)
        ):
            raise ValueError(
                "workers.tools.shell=false is not enforceable by the built-in Codex CLI "
                "adapter; use Claude Code or an owner-supplied worker adapter for a "
                "no-shell experiment."
            )


@dataclasses.dataclass(frozen=True)
class IntegrityReviewConfig:
    """Mandatory semantic gate applied before controller validation."""

    component: ComponentConfig = dataclasses.field(default_factory=_default_integrity_reviewer)
    worker: WorkerConfig | None = None
    allow_same_model: bool = False
    allow_unsafe_stub: bool = False
    retries: int = 1
    strict: bool = False
    timeout_seconds: int = 600
    token_budget: int | None = 4_000
    max_budget_usd: float | None = None

    def validate(self, workers: WorkersConfig) -> None:
        if self.retries < 0:
            raise ValueError("integrity_review.retries cannot be negative.")
        if self.timeout_seconds < 1:
            raise ValueError("integrity_review.timeout_seconds must be positive.")
        if self.token_budget is not None and self.token_budget < 1:
            raise ValueError("integrity_review.token_budget must be positive when set.")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise ValueError("integrity_review.max_budget_usd must be positive when set.")
        if self.component.name == "unsafe_approve":
            if not self.allow_unsafe_stub:
                raise ValueError(
                    "integrity_review.component=unsafe_approve requires "
                    "allow_unsafe_stub=true."
                )
            return
        if self.allow_unsafe_stub:
            raise ValueError(
                "integrity_review.allow_unsafe_stub is only valid with unsafe_approve."
            )
        reviewer = self.worker
        if reviewer is None:
            if not self.allow_same_model:
                raise ValueError(
                    "A dedicated integrity_review.worker is required unless "
                    "allow_same_model=true is explicitly set."
                )
            reviewer = workers.pool[0]
        reviewer.validate()
        mutation_identities = {(item.harness, item.model) for item in workers.pool}
        if (
            (reviewer.harness, reviewer.model) in mutation_identities
            and not self.allow_same_model
        ):
            raise ValueError(
                "The integrity reviewer must use a model outside the mutation pool; "
                "set allow_same_model=true only for an explicit ablation."
            )

    def resolved_worker(self, workers: WorkersConfig) -> WorkerConfig | None:
        """Resolve the separately budgeted reviewer worker after validation."""

        if self.component.name == "unsafe_approve":
            return None
        return self.worker or workers.pool[0]


@dataclasses.dataclass(frozen=True)
class SandboxConfig:
    """Coding-agent sandbox settings."""

    backend: str = "docker"
    worker_image: str = "optiprofiler-evolve-worker:latest"
    gateway_image: str = "optiprofiler-evolve-gateway:latest"
    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512
    gateway_cpus: float = 0.5
    gateway_memory: str = "256m"
    gateway_pids_limit: int = 64
    gateway_start_timeout_seconds: int = 30
    max_candidate_files: int = 2000
    max_candidate_bytes: int = 200_000_000
    allow_direct_network: bool = False

    def validate(self) -> None:
        if self.backend not in {"docker", "unsafe_local"}:
            raise ValueError("sandbox.backend must be 'docker' or 'unsafe_local'.")
        if self.cpus <= 0 or self.pids_limit < 16 or not self.memory:
            raise ValueError("sandbox cpus/memory must be set and pids_limit must be at least 16.")
        if (
            not self.gateway_image
            or self.gateway_cpus <= 0
            or not self.gateway_memory
            or self.gateway_pids_limit < 16
            or self.gateway_start_timeout_seconds < 1
        ):
            raise ValueError(
                "sandbox gateway image/resources/start timeout must be set, and "
                "gateway_pids_limit must be at least 16."
            )
        if self.max_candidate_files < 1 or self.max_candidate_bytes < 1:
            raise ValueError("sandbox candidate file and byte limits must be positive.")


@dataclasses.dataclass(frozen=True)
class EvolveConfig:
    """Complete experiment configuration."""

    data: DataConfig = dataclasses.field(default_factory=DataConfig)
    evaluation: EvaluationConfig = dataclasses.field(default_factory=EvaluationConfig)
    evolution: EvolutionConfig = dataclasses.field(default_factory=EvolutionConfig)
    workers: WorkersConfig = dataclasses.field(default_factory=WorkersConfig)
    integrity_review: IntegrityReviewConfig = dataclasses.field(
        default_factory=IntegrityReviewConfig
    )
    sandbox: SandboxConfig = dataclasses.field(default_factory=SandboxConfig)
    workflow: WorkflowConfig = dataclasses.field(default_factory=WorkflowConfig)

    def __post_init__(self) -> None:
        policies = tuple(
            dataclasses.replace(
                policy,
                options={"interval": self.evolution.migration_interval},
            )
            if policy.name == "migration" and not policy.options
            else policy
            for policy in self.workflow.after_iteration
        )
        if policies != self.workflow.after_iteration:
            object.__setattr__(
                self,
                "workflow",
                dataclasses.replace(self.workflow, after_iteration=policies),
            )

    def validate(self) -> None:
        self.data.validate()
        self.evaluation.validate()
        self.evolution.validate()
        self.workers.validate()
        self.integrity_review.validate(self.workers)
        self.sandbox.validate()
        self.workflow.validate()
        workers = list(self.workers.pool)
        reviewer = self.integrity_review.resolved_worker(self.workers)
        if reviewer is not None:
            workers.append(reviewer)
        gateway_workers = [worker for worker in workers if worker.provider_gateway is not None]
        if gateway_workers and self.workers.adapter != "cli":
            raise ValueError(
                "provider_gateway currently requires workers.adapter='cli'; custom "
                "worker adapters cannot claim the built-in gateway boundary."
            )
        if self.sandbox.backend == "unsafe_local":
            if gateway_workers:
                raise ValueError(
                    "provider_gateway requires the Docker sandbox; unsafe_local remains direct."
                )
            return
        direct_workers = [worker for worker in workers if worker.provider_gateway is None]
        if direct_workers and not (
            self.workers.allow_direct_provider and self.sandbox.allow_direct_network
        ):
            identities = sorted({f"{worker.harness}:{worker.model}" for worker in direct_workers})
            raise ValueError(
                "Docker workers without provider_gateway require both "
                "workers.allow_direct_provider=true and "
                "sandbox.allow_direct_network=true; direct workers: "
                f"{identities!r}"
            )

    def redacted_dict(self) -> dict[str, Any]:
        """Return a serializable config without credential values."""

        result = plain_data(self)
        for worker in result["workers"]["pool"]:
            worker["env"] = {
                key: "<redacted>" if _is_secret_name(key) else value
                for key, value in worker["env"].items()
            }
        reviewer = result["integrity_review"].get("worker")
        if reviewer is not None:
            reviewer["env"] = {
                key: "<redacted>" if _is_secret_name(key) else value
                for key, value in reviewer["env"].items()
            }
        return result

    def resolved_dict(self) -> dict[str, Any]:
        """Return the canonical serializable configuration used for provenance."""

        return self.redacted_dict()

    def without_step(self, name: str) -> EvolveConfig:
        """Return a config with one named attempt step removed."""

        steps = tuple(step for step in self.workflow.attempt_steps if step.name != name)
        if len(steps) == len(self.workflow.attempt_steps):
            raise KeyError(f"Attempt step {name!r} is not configured.")
        return dataclasses.replace(
            self,
            workflow=dataclasses.replace(self.workflow, attempt_steps=steps),
        )

    def without_phase(self, name: str) -> EvolveConfig:
        """Return a config with one optional outer phase removed."""

        if name in {"prepare", "explore", "validate", "hidden", "report"}:
            raise ValueError(f"Core phase {name!r} cannot be removed.")
        phases = tuple(phase for phase in self.workflow.phases if phase.name != name)
        if len(phases) == len(self.workflow.phases):
            raise KeyError(f"Phase {name!r} is not configured.")
        return dataclasses.replace(
            self,
            workflow=dataclasses.replace(self.workflow, phases=phases),
        )

    def with_phase(
        self,
        phase: ComponentConfig | object,
        *,
        after: str,
    ) -> EvolveConfig:
        """Return a config with one registered or trusted phase inserted."""

        component = (
            phase if isinstance(phase, ComponentConfig) else ComponentConfig.from_component(phase)
        )
        phases = list(self.workflow.phases)
        if any(existing.name == component.name for existing in phases):
            raise ValueError(f"Phase {component.name!r} is already configured.")
        phases.insert(_component_index(phases, after) + 1, component)
        return dataclasses.replace(
            self,
            workflow=dataclasses.replace(self.workflow, phases=tuple(phases)),
        )

    def with_phase_options(self, name: str, **options: object) -> EvolveConfig:
        """Return a config with immutable replacement options for one phase."""

        if not options:
            raise ValueError("with_phase_options requires at least one option.")
        found = False
        phases = []
        for phase in self.workflow.phases:
            if phase.name == name:
                found = True
                phases.append(dataclasses.replace(phase, options=options))
            else:
                phases.append(phase)
        if not found:
            raise KeyError(f"Phase {name!r} is not configured.")
        return dataclasses.replace(
            self,
            workflow=dataclasses.replace(self.workflow, phases=tuple(phases)),
        )

    def with_step(
        self,
        step: ComponentConfig | object,
        *,
        after: str | None = None,
    ) -> EvolveConfig:
        """Return a config with a registered or trusted step inserted."""

        component = (
            step if isinstance(step, ComponentConfig) else ComponentConfig.from_component(step)
        )
        steps = list(self.workflow.attempt_steps)
        if any(existing.name == component.name for existing in steps):
            raise ValueError(f"Attempt step {component.name!r} is already configured.")
        index = len(steps) if after is None else _component_index(steps, after) + 1
        steps.insert(index, component)
        return dataclasses.replace(
            self,
            workflow=dataclasses.replace(self.workflow, attempt_steps=tuple(steps)),
        )

    def reorder_steps(self, names: Sequence[str]) -> EvolveConfig:
        """Return a config with exactly the same steps in a new order."""

        current = {step.name: step for step in self.workflow.attempt_steps}
        if len(names) != len(set(names)) or set(names) != set(current):
            raise ValueError("reorder_steps must list every configured step exactly once.")
        ordered = tuple(current[name] for name in names)
        return dataclasses.replace(
            self,
            workflow=dataclasses.replace(self.workflow, attempt_steps=ordered),
        )

    def with_worker(self, harness: str, *, model: str | None = None) -> EvolveConfig:
        """Return a single-worker variant while preserving common worker budgets."""

        if not self.workers.pool:
            raise ValueError("Cannot replace an empty worker pool.")
        template = self.workers.pool[0]
        worker = dataclasses.replace(template, harness=harness, model=model or template.model)
        return dataclasses.replace(
            self,
            workers=dataclasses.replace(self.workers, pool=(worker,)),
        )

    def with_evaluator(self, adapter: str) -> EvolveConfig:
        """Return a config that resolves a different trusted evaluator factory."""

        return dataclasses.replace(
            self,
            evaluation=dataclasses.replace(self.evaluation, adapter=adapter),
        )

    def with_iterations(self, iterations: int) -> EvolveConfig:
        """Return a config with a different evolution length."""

        return dataclasses.replace(
            self,
            evolution=dataclasses.replace(self.evolution, iterations=iterations),
        )


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
        normalized = _normalize_legacy_config(dict(raw))
        loaded = _from_mapping(EvolveConfig, _expand_environment(normalized), "config")
    loaded.validate()
    return loaded


def _expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"Required environment variable {name!r} is not set.")
            return os.environ[name]

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, Mapping):
        return {str(key): _expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    return value


def _from_mapping(cls: type[_T], raw: Mapping[str, Any], path: str) -> _T:
    fields = {field.name: field for field in dataclasses.fields(cls)}
    private = sorted(key for key in raw if str(key).startswith("_"))
    if private:
        raise ValueError(f"Private keys are not allowed at {path}: {private!r}")
    unknown = sorted(set(raw).difference(fields))
    if unknown:
        raise ValueError(f"Unknown keys at {path}: {unknown!r}")

    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        annotation = hints[name]
        nested = _nested_dataclass(annotation)
        if nested is not None:
            if value is None and type(None) in get_args(annotation):
                kwargs[name] = None
                continue
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
    if get_origin(annotation) in {types.UnionType, Union}:
        for option in get_args(annotation):
            if isinstance(option, type) and dataclasses.is_dataclass(option):
                return option
    return None


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(
        marker in upper
        for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    )


def _normalize_legacy_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate early-alpha spellings to their explicit canonical names."""

    evolution = raw.get("evolution")
    if isinstance(evolution, Mapping) and "rounds" in evolution:
        if "iterations" in evolution:
            raise ValueError("Use only evolution.iterations; rounds is a legacy alias.")
        translated = dict(evolution)
        translated["iterations"] = translated.pop("rounds")
        raw["evolution"] = translated
    evaluation = raw.get("evaluation")
    if isinstance(evaluation, Mapping) and evaluation.get("backend") == "local":
        translated = dict(evaluation)
        translated["backend"] = "unsafe_local"
        raw["evaluation"] = translated
    return raw


def _freeze_value(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def plain_data(value: Any) -> Any:
    """Convert frozen config values into JSON-native containers."""

    if dataclasses.is_dataclass(value):
        return {
            field.name: plain_data(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(key): plain_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain_data(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _validate_unique_components(components: Sequence[ComponentConfig], path: str) -> None:
    names = [component.name for component in components]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Duplicate components at {path}: {duplicates!r}")


def _component_index(components: Sequence[ComponentConfig], name: str) -> int:
    for index, component in enumerate(components):
        if component.name == name:
            return index
    raise KeyError(f"Component {name!r} is not configured.")
