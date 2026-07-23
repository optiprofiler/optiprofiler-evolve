"""Credential separation and deterministic CLI routing for provider gateways."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import ProviderGatewayConfig, WorkerConfig
from .provider_gateway import GatewayRoute


_DUMMY_CREDENTIAL = "ope-gateway-placeholder-not-a-provider-secret"
_CODEX_DUMMY_ENV = "OPTIPROFILER_EVOLVE_GATEWAY_DUMMY_KEY"


@dataclass(frozen=True)
class ProviderTransportPlan:
    """Separated worker and sidecar inputs for one provider invocation."""

    worker: WorkerConfig
    route: GatewayRoute = field(repr=False)
    credential_env: str


def prepare_provider_transport(
    worker: WorkerConfig,
    selected_values: Mapping[str, str],
    *,
    gateway_origin: str,
) -> ProviderTransportPlan:
    """Remove real credentials from a worker and pin its CLI to the gateway."""

    config = worker.provider_gateway
    if config is None:
        raise ValueError("Worker has no provider_gateway configuration.")
    worker.validate()
    gateway_origin = _validated_gateway_origin(gateway_origin)
    try:
        credential = selected_values[config.credential_env]
    except KeyError as exc:
        raise ValueError(
            f"Provider credential {config.credential_env!r} was not resolved."
        ) from exc
    if not credential:
        raise ValueError("Provider credential cannot be empty.")

    sensitive = {
        key
        for key in selected_values
        if _looks_secret(key) or key in worker.pass_env
    }
    sensitive.add(config.credential_env)
    unexpected = sensitive.difference({config.credential_env})
    if unexpected:
        raise ValueError(
            "A gateway-routed worker cannot receive unrelated secret/pass_env values: "
            f"{sorted(unexpected)!r}"
        )
    safe_values = {
        key: value
        for key, value in selected_values.items()
        if key not in sensitive
    }
    protocol = config.resolved_protocol(worker.harness)
    auth_mode = _resolved_auth_mode(config, protocol)
    route = GatewayRoute(
        protocol=protocol,
        upstream_base_url=config.upstream_base_url,
        credential=credential,
        auth_mode=auth_mode,
        max_request_bytes=config.max_request_bytes,
        connect_timeout_seconds=config.connect_timeout_seconds,
        response_timeout_seconds=config.response_timeout_seconds,
    )

    if worker.harness == "claude":
        safe_values.update(
            {
                "ANTHROPIC_BASE_URL": gateway_origin,
                "ANTHROPIC_API_KEY": _DUMMY_CREDENTIAL,
            }
        )
        routed = dataclasses.replace(
            worker,
            env=safe_values,
            pass_env=(),
            provider_gateway=None,
        )
    else:
        safe_values[_CODEX_DUMMY_ENV] = _DUMMY_CREDENTIAL
        routed = dataclasses.replace(
            worker,
            env=safe_values,
            pass_env=(),
            args=worker.args + _codex_gateway_args(gateway_origin),
            provider_gateway=None,
        )
    _assert_credential_absent(routed, credential)
    return ProviderTransportPlan(
        worker=routed,
        route=route,
        credential_env=config.credential_env,
    )


def _codex_gateway_args(gateway_origin: str) -> tuple[str, ...]:
    base_url = gateway_origin.rstrip("/") + "/v1"
    return (
        "--ignore-user-config",
        "--strict-config",
        "--config",
        'model_provider="optiprofiler_gateway"',
        "--config",
        'model_providers.optiprofiler_gateway.name="OptiProfiler Evolve gateway"',
        "--config",
        f'model_providers.optiprofiler_gateway.base_url="{base_url}"',
        "--config",
        f'model_providers.optiprofiler_gateway.env_key="{_CODEX_DUMMY_ENV}"',
        "--config",
        'model_providers.optiprofiler_gateway.wire_api="responses"',
    )


def _validated_gateway_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Internal provider gateway origin must be an http URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {
        "",
        "/",
    }:
        raise ValueError("Internal provider gateway origin must not contain a path or credentials.")
    return value.rstrip("/")


def _resolved_auth_mode(config: ProviderGatewayConfig, protocol: str) -> str:
    if config.auth_mode != "auto":
        return config.auth_mode
    if protocol == "anthropic" and "AUTH_TOKEN" not in config.credential_env.upper():
        return "x-api-key"
    return "bearer"


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(
        marker in upper
        for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    )


def _assert_credential_absent(worker: WorkerConfig, credential: str) -> None:
    surfaces = [*worker.env.values(), *worker.args]
    if any(credential in value for value in surfaces):
        raise RuntimeError("Provider credential remained on a worker-visible surface.")


__all__: list[str] = []
