"""Versioned research artifacts used by the optional full evolution workflow."""

from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


DIRECTION_SCHEMA = "direction_cards/1"
STRATEGY_SCHEMA = "strategy_cards/1"
ABLATION_SCHEMA = "strategy_ablation/1"
BUNDLE_SCHEMA = "island_bundles/1"
RECOMBINATION_SCHEMA = "recombination/1"
CHALLENGER_SCHEMA = "challenger_report/1"
EVIDENCE_SCHEMA = "benchmark_evidence/1"


def read_json_object(path: Path) -> dict[str, Any]:
    """Read one JSON object with a path-specific error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON object from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def write_json(path: Path, value: object) -> Path:
    """Atomically write one deterministic JSON artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def safe_relative_path(value: str) -> PurePosixPath:
    """Validate one role-workspace relative path."""

    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value or value == ".":
        raise ValueError(f"Unsafe relative path: {value!r}")
    return path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_diff(before: Path, after: Path) -> str:
    """Return a bounded, deterministic unified diff between two solver trees."""

    names = sorted(_text_files(before).union(_text_files(after)))
    chunks: list[str] = []
    for name in names:
        left = _read_text(before / name)
        right = _read_text(after / name)
        if left == right:
            continue
        chunks.extend(
            difflib.unified_diff(
                left.splitlines(keepends=True),
                right.splitlines(keepends=True),
                fromfile=f"seed/{name}",
                tofile=f"finalist/{name}",
            )
        )
    return "".join(chunks) or "# No text-file differences were detected.\n"


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


class EvidenceReader:
    """Create a small index over existing OptiProfiler benchmark outputs.

    The reader prefers a future versioned ``agent_report.json`` but does not
    invent benchmark statistics when only the current artifact bundle exists.
    """

    def build(self, source: Path, destination: Path) -> Path:
        source = source.resolve()
        report = source / "agent_report.json"
        if report.is_file():
            payload = {
                "schema": EVIDENCE_SCHEMA,
                "source": "agent_report",
                "agent_report": read_json_object(report),
                "artifacts": self._index(source),
            }
        else:
            payload = {
                "schema": EVIDENCE_SCHEMA,
                "source": "legacy_bundle",
                "result": _optional_json(source / "result.json"),
                "profile_scores": _optional_json(source / "profile_scores.json"),
                "artifact_index": _optional_json(source / "artifact_index.json"),
                "artifacts": self._index(source),
                "limitations": [
                    "No versioned OptiProfiler agent_report.json was available.",
                    "This index does not infer problem-level causes from filenames or plots.",
                ],
            }
        return write_json(destination, payload)

    @staticmethod
    def _index(root: Path) -> list[dict[str, Any]]:
        entries = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            media_type, _encoding = mimetypes.guess_type(path.name)
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "media_type": media_type or "application/octet-stream",
                }
            )
        return entries


def _text_files(root: Path) -> set[str]:
    names: set[str] = set()
    if not root.is_dir():
        return names
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_size <= 2_000_000:
            try:
                path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            names.add(path.relative_to(root).as_posix())
    return names


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _optional_json(path: Path) -> object | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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


__all__: list[str] = []
