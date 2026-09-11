"""Versioned research artifacts and normalization of untrusted strategy cards."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from optiprofiler_evolve.config import EvolveConfig, ToolConfig
from optiprofiler_evolve.files import read_json_object, safe_relative_path
from optiprofiler_evolve.phases.contracts import PhaseContext
from optiprofiler_evolve.phases.prepare import PreparedArtifact

DIRECTION_SCHEMA = "direction_cards/1"


STRATEGY_SCHEMA = "strategy_cards/1"


ABLATION_SCHEMA = "strategy_ablation/1"


BUNDLE_SCHEMA = "island_bundles/1"


RECOMBINATION_SCHEMA = "recombination/1"


STRATEGY_TRANSFER_SCHEMA = "strategy_transfer/1"


CHALLENGER_SCHEMA = "challenger_report/1"


def normalize_direction_cards(payload: Mapping[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """Validate and normalize untrusted scout output."""

    raw_cards = payload.get("cards", [])
    if not isinstance(raw_cards, list):
        raise ValueError("direction cards must be a list")
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cards[:limit]):
        if not isinstance(raw, Mapping):
            raise ValueError("each direction card must be an object")
        card_id = _identifier(raw.get("card_id"), f"d{index + 1}")
        if card_id in seen:
            raise ValueError(f"duplicate direction card id: {card_id}")
        seen.add(card_id)
        title = _short_text(raw.get("title"), "Untitled direction")
        hypothesis = _short_text(raw.get("hypothesis"), title)
        tactics = _string_list(raw.get("tactics", []), maximum=8)
        citations = []
        for citation in _mapping_list(raw.get("citations", []), maximum=12):
            url = _short_text(citation.get("url"), "")
            claim = _short_text(citation.get("claim"), "")
            if url:
                citations.append({"url": url, "claim": claim})
        cards.append(
            {
                "card_id": card_id,
                "title": title,
                "hypothesis": hypothesis,
                "tactics": tactics,
                "citations": citations,
            }
        )
    return cards


def normalize_strategy_cards(
    payload: Mapping[str, Any],
    *,
    island: int,
    finalist: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Validate untrusted analyst hypotheses without accepting evidence claims."""

    raw_cards = payload.get("cards", [])
    if not isinstance(raw_cards, list):
        raise ValueError("strategy cards must be a list")
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cards[:limit]):
        if not isinstance(raw, Mapping):
            raise ValueError("each strategy card must be an object")
        strategy_id = _identifier(raw.get("strategy_id"), f"i{island}-s{index + 1}")
        if strategy_id in seen:
            raise ValueError(f"duplicate strategy id: {strategy_id}")
        seen.add(strategy_id)
        toggle = raw.get("toggle")
        normalized_toggle: dict[str, str] | None = None
        if isinstance(toggle, Mapping):
            kind = str(toggle.get("kind", ""))
            if kind in {"removal_patch", "variant_tree"} and toggle.get("ref"):
                normalized_toggle = {
                    "kind": kind,
                    "ref": safe_relative_path(str(toggle["ref"])).as_posix(),
                }
        portable = raw.get("portable_patch")
        portable_patch: dict[str, str] | None = None
        if isinstance(portable, Mapping) and portable.get("ref"):
            portable_patch = {
                "ref": safe_relative_path(str(portable["ref"])).as_posix(),
                "base": str(portable.get("base", "seed")),
                "base_tree_hash": str(portable.get("base_tree_hash", "")),
            }
        bindings = []
        for binding in _mapping_list(raw.get("code_bindings", []), maximum=20):
            file_name = safe_relative_path(str(binding.get("file", ""))).as_posix()
            lines = binding.get("lines", [])
            if not (
                isinstance(lines, Sequence)
                and not isinstance(lines, (str, bytes))
                and len(lines) == 2
                and all(isinstance(value, int) and value >= 1 for value in lines)
            ):
                lines = []
            bindings.append({"file": file_name, "lines": list(lines)})
        cards.append(
            {
                "strategy_id": strategy_id,
                "claim": _short_text(raw.get("claim"), "Unspecified strategy"),
                "mechanism": _short_text(raw.get("mechanism"), ""),
                "pseudocode": _string_list(raw.get("pseudocode", []), maximum=30),
                "applicability": _string_list(raw.get("applicability", []), maximum=20),
                "risks": _string_list(raw.get("risks", []), maximum=20),
                "evidence_level": "Inferred",
                "code_bindings": bindings,
                "toggle": normalized_toggle,
                "portable_patch": portable_patch,
                "depends_on": _string_list(raw.get("depends_on", []), maximum=20),
                "sources": _string_list(raw.get("sources", []), maximum=20),
                "finalist": finalist,
            }
        )
    return cards


def _identifier(value: object, fallback: str) -> str:
    text = str(value or fallback)
    if not text.replace("-", "_").isidentifier():
        raise ValueError(f"invalid identifier: {text!r}")
    return text


def _short_text(value: object, fallback: str) -> str:
    text = str(value if value is not None else fallback).strip()
    return text[:4000]


def _string_list(value: object, *, maximum: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [_short_text(item, "") for item in value[:maximum] if _short_text(item, "")]


def _mapping_list(value: object, *, maximum: int) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value[:maximum] if isinstance(item, Mapping)]


def _prepared(context: PhaseContext) -> PreparedArtifact:
    value = context.artifacts.get("prepared")
    if not isinstance(value, PreparedArtifact):
        raise RuntimeError("research phase requires the structured prepared artifact")
    return value


def _require_public_performance(config: EvolveConfig, phase: str) -> None:
    if not config.assessment.enabled("performance", "public"):
        raise ValueError(f"Phase {phase!r} requires public performance assessment.")


def _tool_config(
    value: Mapping[str, object] | None,
    *,
    network: bool,
    web_search: bool,
) -> ToolConfig:
    defaults: dict[str, object] = {
        "preset": "research",
        "network": network,
        "web_search": web_search,
        "shell": True,
        "python": True,
        "git": True,
        "compilers": False,
        "package_install": False,
        "communication": "none",
    }
    defaults.update(dict(value or {}))
    tools = ToolConfig(**defaults)
    tools.validate()
    return tools


def _tool_payload(tools: ToolConfig) -> dict[str, object]:
    return {
        "preset": tools.preset,
        "web_search": tools.web_search,
        "network": tools.network,
        "shell": tools.shell,
        "python": tools.python,
        "git": tools.git,
        "compilers": tools.compilers,
        "package_install": tools.package_install,
        "communication": tools.communication,
    }


def supported_strategy_cards(paths: Sequence[object]) -> Iterator[tuple[Path, int, Mapping[str, Any]]]:
    """Read only controller-supported strategies for transfer and recombination."""
    for path in paths:
        payload = read_json_object(Path(str(path)))
        island = int(payload["island"])
        for card in payload.get("cards", []):
            if not isinstance(card, Mapping):
                continue
            ablation = card.get("ablation")
            if isinstance(ablation, Mapping) and ablation.get("conclusion") == "supported":
                yield Path(str(path)), island, card
