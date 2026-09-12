"""Index existing benchmark artifacts without inventing performance evidence."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from optiprofiler_evolve.files import write_json


class EvidenceReader:
    """Index already-projected benchmark files without inferring missing statistics."""

    def build(self, source: Path, destination: Path) -> Path:
        source = source.resolve()
        payload = {
            "schema": EVIDENCE_SCHEMA,
            "source": "legacy_bundle",
            "result": _optional_json(source / "result.json"),
            "profile_scores": _optional_json(source / "profile_scores.json"),
            "artifact_index": _optional_json(source / "artifact_index.json"),
            "artifacts": self._index(source),
            "limitations": [
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


def _optional_json(path: Path) -> object | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


EVIDENCE_SCHEMA = "benchmark_evidence/1"
