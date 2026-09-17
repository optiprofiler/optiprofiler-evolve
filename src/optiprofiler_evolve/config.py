"""Strict configuration for an OptiProfiler Evolve experiment."""

from __future__ import annotations

import dataclasses
import math
import os
import re
import types
import warnings
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from functools import cached_property
from pathlib import Path
from typing import Any, Generic, TypeVar, Union, get_args, get_origin, get_type_hints
from urllib.parse import unquote, urlsplit

import yaml

from optiprofiler_evolve.criteria.performance.competitors import competitor_names

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_T = TypeVar("_T")
_K = TypeVar("_K")
_V = TypeVar("_V")
ASSESSMENT_PROFILES = ("smoke", "public", "validation", "hidden")
# Owner login files a harness may receive as a read-only mount instead of a gateway.
SUBSCRIPTION_LOGIN_ENV = {
    "codex": "CODEX_AUTH_JSON_PATH",
    "claude": "CLAUDE_CREDENTIALS_JSON_PATH",
}


class FrozenDict(Mapping[_K, _V], Generic[_K, _V]):
    """A read-only mapping; configuration boundaries recursively freeze nested values."""

    def __init__(self, value: Mapping[_K, _V] | None = None) -> None:
        self._data = dict(value or {})

    def __getitem__(self, key: _K) -> _V:
        return self._data[key]

    def __iter__(self) -> Iterator[_K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __deepcopy__(self, memo: dict[int, object]) -> FrozenDict[_K, _V]:
        copied = type(self)()
        memo[id(self)] = copied
        copied._data = deepcopy(self._data, memo)
        return copied

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
        object.__setattr__(self, "options", freeze_value(self.options))

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
        for name in ("mutate", "smoke", "public_evaluate")
    )


def _default_after_iteration() -> tuple[ComponentConfig, ...]:
    return ()


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
        _validate_unique_components(self.attempt_steps, "workflow.attempt_steps")
        _validate_unique_components(self.after_iteration, "workflow.after_iteration")


@dataclasses.dataclass(frozen=True)
class SplitConfig:
    """Deterministic public/validation/hidden/smoke split settings."""

    validation_fraction: float = 0.2
    hidden_fraction: float = 0.2
    smoke_count: int = 3
    seed: int = 0
    lockbox_required: bool = False
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
        if self.lockbox_required and (
            (explicit and not self.hidden)
            or (not explicit and self.hidden_fraction == 0)
        ):
            raise ValueError(
                "A required lockbox requires a nonempty hidden split."
            )
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
        object.__setattr__(self, "selection", freeze_value(self.selection))

    def validate(self) -> None:
        if not self.library or not self.library.isidentifier():
            raise ValueError("data.library must be a non-empty Python identifier.")
        if len(set(self.problem_names)) != len(self.problem_names):
            raise ValueError("data.problem_names contains duplicates.")
        self.split.validate()


@dataclasses.dataclass(frozen=True)
class EvaluationExecutionConfig:
    """Execution resources, public projection, and broker quotas."""

    backend: str = "docker"
    docker_image: str | None = None
    solver_image: str = "optiprofiler-evolve-solver:latest"
    timeout_seconds: int = 3600
    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512
    feedback_mode: str = "score_only"
    forbidden_candidate_imports: tuple[str, ...] = ()
    max_smoke_calls_per_worker: int = 20
    max_public_calls_per_worker: int = 5
    adapter: str = "optiprofiler"


    def validate(self) -> None:
        if self.backend not in {"docker", "unsafe_local"}:
            raise ValueError("evaluation.backend must be 'docker' or 'unsafe_local'.")
        if self.timeout_seconds < 1:
            raise ValueError("evaluation.timeout_seconds must be positive.")
        if not self.solver_image:
            raise ValueError("evaluation.solver_image must name a numeric-only runtime image.")
        if self.cpus <= 0 or self.pids_limit < 16 or not self.memory:
            raise ValueError(
                "evaluation cpus/memory must be set and pids_limit must be at least 16."
            )
        if self.feedback_mode not in {
            "score_only",
            "full_artifacts",
        }:
            raise ValueError(
                "evaluation.feedback_mode must be 'score_only' or 'full_artifacts'."
            )
        if not self.adapter or not self.adapter.replace("-", "_").isidentifier():
            raise ValueError("evaluation.adapter must be a registered component name.")
        for name in self.forbidden_candidate_imports:
            if not name or any(not part.isidentifier() for part in name.split(".")):
                raise ValueError(
                    "evaluation.forbidden_candidate_imports must contain dotted "
                    "Python module names."
                )
        if self.max_smoke_calls_per_worker < 0 or self.max_public_calls_per_worker < 0:
            raise ValueError("Evaluation call quotas cannot be negative.")

@dataclasses.dataclass(frozen=True)
class EvaluationConfig(EvaluationExecutionConfig):
    """Resolved backend request; quality options come from a criterion profile."""

    benchmark: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    smoke_overrides: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    reference: str = "initial"
    fitness_source: str = "solver_scores"

    def __post_init__(self) -> None:
        object.__setattr__(self, "benchmark", freeze_value(self.benchmark))
        object.__setattr__(self, "smoke_overrides", freeze_value(self.smoke_overrides))

    def validate(self) -> None:
        super().validate()
        if self.backend == "docker" and not self.docker_image:
            raise ValueError("evaluation.docker_image is required for Docker evaluation.")
        if self.reference != "initial" and self.reference not in competitor_names():
            raise ValueError(
                "evaluation.reference must be 'initial' or a registered DFO competitor."
            )
        if self.fitness_source not in {"solver_scores", "profile_scores_mean"}:
            raise ValueError(
                "evaluation.fitness_source must be 'solver_scores' or "
                "'profile_scores_mean'."
            )
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

    iterations: int = 1
    islands: int = 1
    attempts_per_island: int = 1
    population_per_island: int = 2
    # Whole-candidate migration is opt-in. Keeping islands independent by
    # default makes strategy attribution and later ablations easier to audit.
    migration_interval: int = 0
    random_seed: int = 0
    finalists_per_island: int = 1
    retention: ComponentConfig = dataclasses.field(default_factory=_default_retention)
    parent_sampler: ComponentConfig = dataclasses.field(default_factory=_default_parent_sampler)

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
        object.__setattr__(self, "env", freeze_value(self.env))

    @property
    def uses_subscription_login(self) -> bool:
        """Whether this worker names its own harness's owner login file."""
        return SUBSCRIPTION_LOGIN_ENV.get(self.harness) in {*self.env, *self.pass_env}

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

    preset: str = "minimal"
    web_search: bool = False
    network: bool = True
    shell: bool = True
    python: bool = True
    git: bool = True
    compilers: bool = False
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
    prompt_note: str | None = None
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
        if self.prompt_note is not None:
            if not isinstance(self.prompt_note, str) or not self.prompt_note.strip():
                raise ValueError("workers.prompt_note must be a non-empty string when set.")
            if len(self.prompt_note) > 2000:
                raise ValueError("workers.prompt_note must be at most 2000 characters.")
        if not self.adapter or not self.adapter.replace("-", "_").isidentifier():
            raise ValueError("workers.adapter must be a registered component name.")
        for worker in self.pool:
            worker.validate()
        self.tools.validate()
        if (
            self.adapter in {"cli", "harbor"}
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
class ComplexityConfig:
    """Seed-relative source complexity budget, separate from fitness."""

    mode: str = "warn"
    max_ratio: float = 1.2

    def validate(self) -> None:
        if self.mode not in {"warn", "enforce"}:
            raise ValueError("complexity.mode must be 'warn' or 'enforce'.")
        if (
            isinstance(self.max_ratio, bool)
            or not isinstance(self.max_ratio, (int, float))
            or not math.isfinite(self.max_ratio)
            or self.max_ratio <= 0
        ):
            raise ValueError("complexity.max_ratio must be a finite positive number.")


@dataclasses.dataclass(frozen=True)
class CriterionConfig:
    enabled: bool = True
    weight: float = 1.0
    mode: str = "enforce"
    options: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("Criterion enabled must be boolean.")
        if (isinstance(self.weight, bool) or not isinstance(self.weight, (int, float))
                or not math.isfinite(self.weight) or self.weight < 0):
            raise ValueError("Criterion weight must be finite and nonnegative.")
        if self.mode not in {"warn", "enforce"}:
            raise ValueError("Criterion mode must be warn or enforce.")
        if not isinstance(self.options, Mapping):
            raise ValueError("Criterion options must be a mapping.")
        object.__setattr__(self, "options", freeze_value(self.options))


def _default_criteria() -> Mapping[str, CriterionConfig]:
    return {
        "performance": CriterionConfig(),
        "complexity": CriterionConfig(enabled=False, mode="warn"),
        "integrity": CriterionConfig(weight=0.0),
    }


# Criteria whose options are validated against a strict dataclass. A disabled
# criterion is usually not reached by the enabled-only option validation, so
# without this an omitted or misspelled key would be stored verbatim and only
# surface later, when the criterion is re-enabled.
_CRITERION_OPTION_SCHEMAS: Mapping[str, type] = {
    "complexity": ComplexityConfig,
    "integrity": IntegrityReviewConfig,
}

# Option keys retired by this branch, reported by name so a migration is told
# that the setting no longer has any effect instead of silently keeping it.
_RETIRED_CRITERION_OPTIONS: Mapping[str, frozenset[str]] = {
    "integrity": frozenset({"strict"}),
}


# Option keys that exist as fields on the option schema but must not be written
# there, mapped to the reason. `mode` is a criterion-level field: the enabled-only
# guard below already rejects it, which meant a disabled criterion silently
# accepted it and then hard-failed the moment it was turned on.
_MISPLACED_CRITERION_OPTIONS: Mapping[str, Mapping[str, str]] = {
    "complexity": {
        "mode": "Complexity mode belongs on the criterion rather than in options.",
    },
}


def _validate_criterion_option_keys(name: str, options: Mapping[str, Any]) -> None:
    """Key-check one criterion's options regardless of whether it is enabled."""

    schema = _CRITERION_OPTION_SCHEMAS.get(name)
    if schema is None:
        return
    path = f"assessment.criteria.{name}.options"
    misplaced = sorted(
        _MISPLACED_CRITERION_OPTIONS.get(name, {}).keys() & set(options)
    )
    if misplaced:
        raise ValueError(f"{_MISPLACED_CRITERION_OPTIONS[name][misplaced[0]]} Found in {path}.")
    retired = sorted(_RETIRED_CRITERION_OPTIONS.get(name, frozenset()).intersection(options))
    if retired:
        raise ValueError(
            f"Removed config fields at {path}: {retired}. This key was dropped by this "
            "branch and no longer has any effect; delete it."
        )
    known = {field.name for field in dataclasses.fields(schema)}
    unknown = sorted(set(options).difference(known))
    if unknown:
        raise ValueError(f"Unknown keys at {path}: {unknown}")


@dataclasses.dataclass(frozen=True)
class AssessmentConfig:
    criteria: Mapping[str, CriterionConfig] = dataclasses.field(default_factory=_default_criteria)
    profiles: Mapping[str, Mapping[str, Mapping[str, Any]]] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        defaults = _default_criteria()
        criteria = {
            name: value if isinstance(value, CriterionConfig) else _from_mapping(
                CriterionConfig,
                {**plain_data(defaults.get(name, CriterionConfig())), **value},
                f"assessment.criteria.{name}",
            )
            for name, value in self.criteria.items()
        }
        object.__setattr__(self, "criteria", freeze_value(criteria))
        object.__setattr__(self, "profiles", freeze_value(self.profiles))
        # Key-check every criterion, including disabled ones: enabled-only
        # validation would let a disabled criterion accumulate removed or
        # misspelled options that only surface once it is re-enabled.
        for name, spec in criteria.items():
            _validate_criterion_option_keys(name, spec.options)
        for profile, overrides in self.profiles.items():
            if profile not in {"smoke", "public", "validation", "hidden"}:
                raise ValueError(f"Unknown assessment profile: {profile!r}")
            for name, override in overrides.items():
                if name not in criteria:
                    raise ValueError(f"Assessment override references unknown criterion {name!r}.")
                if "enabled" in override and not isinstance(override["enabled"], bool):
                    raise ValueError("Criterion enabled must be boolean.")
                unknown = set(override).difference({"enabled", "weight", "mode", "options"})
                if unknown:
                    raise ValueError(f"Unknown criterion override fields: {sorted(unknown)!r}")
        # Validate all override keys before constructing the cached table.
        self.resolved_profiles

    def resolve(self, profile: str) -> Mapping[str, CriterionConfig]:
        if profile not in ASSESSMENT_PROFILES:
            raise ValueError(f"Unknown assessment profile: {profile!r}")
        return self.resolved_profiles[profile]

    @cached_property
    def resolved_profiles(self) -> Mapping[str, Mapping[str, CriterionConfig]]:
        profiles = {}
        for profile in ASSESSMENT_PROFILES:
            result = {}
            for name, spec in self.criteria.items():
                override = dict(self.profiles.get(profile, {}).get(name, {}))
                enabled = override.pop("enabled", name != "integrity" or profile == "validation")
                options = {**spec.options, **override.pop("options", {})}
                result[name] = dataclasses.replace(
                    spec, enabled=spec.enabled and enabled, options=options, **override
                )
            profiles[profile] = result
        return freeze_value(profiles)

    def enabled(self, name: str, profile: str | None = None) -> bool:
        profiles = (self.resolve(profile),) if profile is not None else self.resolved_profiles.values()
        return any(name in specs and specs[name].enabled for specs in profiles)

    def validate(self) -> None:
        formal = self.resolve("validation")
        if not any(item.enabled and item.weight > 0 for item in formal.values()):
            raise ValueError("Formal assessment needs an enabled criterion with positive weight.")


@dataclasses.dataclass(frozen=True)
class ResolvedAssessmentProfile:
    """Derived execution inputs, excluded from declared configuration and its hash."""

    criteria: Mapping[str, CriterionConfig]
    performance: EvaluationConfig | None
    integrity: IntegrityReviewConfig | None


@dataclasses.dataclass(frozen=True)
class EvolveConfig:
    """Complete experiment configuration."""

    data: DataConfig = dataclasses.field(default_factory=DataConfig)
    execution: EvaluationExecutionConfig = dataclasses.field(default_factory=EvaluationExecutionConfig)
    evolution: EvolutionConfig = dataclasses.field(default_factory=EvolutionConfig)
    workers: WorkersConfig = dataclasses.field(default_factory=WorkersConfig)
    assessment: AssessmentConfig = dataclasses.field(default_factory=AssessmentConfig)
    sandbox: SandboxConfig = dataclasses.field(default_factory=SandboxConfig)
    workflow: WorkflowConfig = dataclasses.field(default_factory=WorkflowConfig)

    @cached_property
    def resolved_profiles(self) -> Mapping[str, ResolvedAssessmentProfile]:
        execution = plain_data(self.execution)
        profiles = {}
        for name, criteria in self.assessment.resolved_profiles.items():
            performance = integrity = None
            spec = criteria.get("performance")
            if spec is not None and spec.enabled:
                forbidden = set(spec.options).intersection(execution)
                if forbidden:
                    raise ValueError(f"Performance options cannot override execution settings: {sorted(forbidden)}")
                performance = _from_mapping(EvaluationConfig, {**execution, **spec.options},
                                            "assessment.criteria.performance.options")
            spec = criteria.get("integrity")
            if spec is not None and spec.enabled:
                integrity = _from_mapping(IntegrityReviewConfig, spec.options,
                                          "assessment.criteria.integrity.options")
            profiles[name] = ResolvedAssessmentProfile(criteria, performance, integrity)
        return freeze_value(profiles)

    @cached_property
    def reference_name(self) -> str | None:
        references = {profile.performance.reference for profile in self.resolved_profiles.values()
                      if profile.performance is not None}
        if len(references) > 1:
            raise ValueError("Performance reference must be fixed across enabled assessment profiles.")
        return next(iter(references), None)

    @cached_property
    def reference_max_eval_factor(self) -> float:
        # A profile's benchmark override must not rewrite the shared reference.
        spec = self.assessment.criteria.get("performance")
        benchmark = spec.options.get("benchmark", {}) if spec is not None else {}
        factor = benchmark.get("max_eval_factor", 500)
        if (isinstance(factor, bool) or not isinstance(factor, (int, float))
                or not math.isfinite(factor) or factor <= 0):
            raise ValueError("Base benchmark.max_eval_factor must be finite and positive.")
        return float(factor)

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
        self.assessment.validate()
        self.workers.validate()
        self.data.validate()
        self.execution.validate()
        workers = list(self.workers.pool)
        for profile in self.resolved_profiles.values():
            if profile.performance is not None:
                profile.performance.validate()
                if (profile.performance.backend == "docker"
                        and profile.performance.solver_image in {
                            profile.performance.docker_image, self.sandbox.worker_image}):
                    raise ValueError("execution.solver_image must be a separate numeric-only image, "
                                     "not the scorer or agent worker image.")
            if profile.integrity is not None:
                review = profile.integrity
                review.validate(self.workers)
                reviewer = review.resolved_worker(self.workers)
                if reviewer is not None:
                    workers.append(reviewer)
            spec = profile.criteria.get("complexity")
            if spec is not None and spec.enabled:
                if "mode" in spec.options:
                    raise ValueError("Complexity mode belongs on the criterion, not inside options.")
                _from_mapping(ComplexityConfig, {**spec.options, "mode": spec.mode},
                              "assessment.criteria.complexity.options").validate()
        if self.reference_name is not None:
            self.reference_max_eval_factor
        self.evolution.validate()
        if (self.evolution.retention.name == "metric_pareto"
                and not self.assessment.enabled("performance", "validation")):
            raise ValueError("metric_pareto requires formal performance metric bundles.")
        self.sandbox.validate()
        self.workflow.validate()
        gateway_workers = [worker for worker in workers if worker.provider_gateway is not None]
        subscription_workers = [worker for worker in workers if worker.uses_subscription_login
                                and worker.provider_gateway is None]
        if self.workers.adapter == "harbor" and (
            self.sandbox.backend != "docker"
            or len(gateway_workers) + len(subscription_workers) != len(workers)
        ):
            raise ValueError("Harbor requires Docker and either a gateway or an explicit subscription login path.")
        if gateway_workers and self.workers.adapter not in {"cli", "harbor"}:
            raise ValueError(
                "provider_gateway requires workers.adapter='cli' or 'harbor'; custom "
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
        def redact(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if (_is_secret_name(key) and not isinstance(item, (int, float, bool))
                            and key not in {"token_budget", "credential_env", "auth_mode"}):
                        value[key] = "<redacted>"
                    else:
                        redact(item)
            elif isinstance(value, list):
                for item in value:
                    redact(item)
        redact(result["assessment"])
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

    def with_criterion(self, name: str, **changes: Any) -> EvolveConfig:
        """Derive a criterion ablation without mutating the shared experiment."""
        current = self.assessment.criteria.get(name, CriterionConfig())
        criteria = {**self.assessment.criteria, name: dataclasses.replace(current, **changes)}
        return dataclasses.replace(self, assessment=dataclasses.replace(self.assessment, criteria=criteria))

    def without_phase(self, name: str) -> EvolveConfig:
        """Remove a phase; dependency validation happens before execution."""
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

    def with_evaluator(self, adapter: str) -> EvolveConfig:
        """Return a config that resolves a different trusted evaluator factory."""

        return dataclasses.replace(
            self,
            execution=dataclasses.replace(self.execution, adapter=adapter),
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
        assessment = _from_mapping(AssessmentConfig, raw.get("assessment", {}), "assessment")
        # A supplied `criteria` map replaces the defaults rather than patching them.
        # That is deliberate - it is how a single standard is exercised in
        # isolation - but it means an omitted criterion is removed rather than
        # disabled, so a gate can disappear with no error. Warn instead of
        # rejecting, because isolating a criterion is a supported use.
        supplied = raw.get("assessment", {}).get("criteria")
        if supplied is not None:
            omitted = sorted(
                name for name, spec in _default_criteria().items()
                if spec.enabled and name not in supplied
            )
            if omitted:
                warnings.warn(
                    f"assessment.criteria omits {omitted}, which the defaults enable. "
                    "This mapping is the complete criterion map, not a patch, so the "
                    "omitted criteria do not run and their gates do not apply. List every "
                    "criterion explicitly (enabled: false to disable one on purpose) to "
                    "silence this warning.",
                    UserWarning,
                    stacklevel=3,
                )
        criteria = {
            name: dataclasses.replace(spec, options=_expand_environment(spec.options))
            if assessment.enabled(name) else spec
            for name, spec in assessment.criteria.items()
        }
        profiles = {
            profile: {
                name: _expand_environment(override)
                if assessment.enabled(name, profile) else override
                for name, override in overrides.items()
            }
            for profile, overrides in assessment.profiles.items()
        }
        expanded = _expand_environment({key: value for key, value in raw.items() if key != "assessment"})
        expanded["assessment"] = plain_data(AssessmentConfig(criteria=criteria, profiles=profiles))
        loaded = _from_mapping(EvolveConfig, expanded, "config")
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
    if cls is EvolveConfig:
        retired = set(raw).intersection({"evaluation", "complexity", "integrity_review"})
        if retired:
            raise ValueError(
                f"Removed config fields {sorted(retired)}. Rename 'evaluation' to 'execution' "
                "and move quality options into 'assessment.criteria'. That mapping is the "
                "COMPLETE criterion map, not a patch, so every criterion you want to run must "
                "be listed; one that is left out does not run at all and its gate is dropped:\n\n"
                "execution: {}            # former 'evaluation', minus the moved quality options\n"
                "assessment:\n"
                "  criteria:\n"
                "    performance: {}      # former evaluation.benchmark / smoke_overrides\n"
                "    integrity: {}        # former 'integrity_review'; omit only to drop the gate\n"
                "    complexity: {enabled: false, mode: warn}"
            )
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


def freeze_value(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict({str(key): freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
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
