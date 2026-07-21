from __future__ import annotations

import dataclasses
import json
import os
import unittest
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from unittest.mock import patch

import jsonschema
import yaml

from optiprofiler_evolve.config import EvolveConfig, load_config
from optiprofiler_evolve.solver import InterfaceSpec, validate_interface


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    def test_schema_and_reference_cover_every_config_field(self) -> None:
        schema = json.loads((ROOT / "config.schema.json").read_text(encoding="utf-8"))
        expected = _dataclass_paths(EvolveConfig)
        documented_by_schema = _schema_paths(schema, schema)
        self.assertEqual(documented_by_schema, expected)

        reference = (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8")
        missing = sorted(path for path in expected if f"| `{path}` |" not in reference)
        self.assertEqual(missing, [])

    def test_checked_in_example_configs_are_valid(self) -> None:
        schema = json.loads((ROOT / "config.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        validator.check_schema(schema)
        environment = {
            "OPTIPROFILER_EVOLVE_MODEL": "test-claude-model",
            "OPTIPROFILER_EVOLVE_CODEX_MODEL": "test-codex-model",
            "OPTIPROFILER_EVOLVE_ANTHROPIC_BASE_URL": "https://example.invalid/anthropic",
            "OPTIPROFILER_EVOLVE_API_KEY": "test-secret",
            "OPTIPROFILER_EVOLVE_OPENAI_BASE_URL": "https://example.invalid/v1",
        }
        with patch.dict(os.environ, environment):
            claude_path = ROOT / "examples" / "experiment.yaml"
            compatible_path = ROOT / "examples" / "experiment-claude-compatible.yaml"
            codex_path = ROOT / "examples" / "experiment-codex.yaml"
            codex_compatible_path = ROOT / "examples" / "experiment-codex-compatible.yaml"
            research_path = ROOT / "examples" / "experiment-research.yaml"
            validator.validate(yaml.safe_load(claude_path.read_text(encoding="utf-8")))
            validator.validate(yaml.safe_load(compatible_path.read_text(encoding="utf-8")))
            validator.validate(yaml.safe_load(codex_path.read_text(encoding="utf-8")))
            validator.validate(
                yaml.safe_load(codex_compatible_path.read_text(encoding="utf-8"))
            )
            validator.validate(yaml.safe_load(research_path.read_text(encoding="utf-8")))
            claude = load_config(claude_path)
            compatible = load_config(compatible_path)
            codex = load_config(codex_path)
            codex_compatible = load_config(codex_compatible_path)
            research = load_config(research_path)
        self.assertEqual(claude.workers.pool[0].harness, "claude")
        self.assertEqual(compatible.workers.pool[0].harness, "claude")
        self.assertEqual(
            compatible.workers.pool[0].env["ANTHROPIC_BASE_URL"],
            "https://example.invalid/anthropic",
        )
        self.assertEqual(
            compatible.redacted_dict()["workers"]["pool"][0]["env"]["ANTHROPIC_AUTH_TOKEN"],
            "<redacted>",
        )
        self.assertEqual(codex.workers.pool[0].harness, "codex")
        self.assertEqual(codex_compatible.workers.pool[0].harness, "codex")
        self.assertIn(
            'model_provider="compatible"',
            codex_compatible.workers.pool[0].args,
        )
        self.assertIn(
            'model_providers.compatible.base_url="https://example.invalid/v1"',
            codex_compatible.workers.pool[0].args,
        )
        self.assertIn("strategy_analysis", [phase.name for phase in research.workflow.phases])

    def test_checked_in_solver_examples_declare_valid_interfaces(self) -> None:
        interface = InterfaceSpec.parse("solver.py:solver")
        validate_interface(ROOT / "examples" / "solver", interface, "python")
        validate_interface(ROOT / "examples" / "repository_solver", interface, "python")


def _dataclass_paths(cls: type[Any], prefix: str = "") -> set[str]:
    hints = get_type_hints(cls)
    paths: set[str] = set()
    for field in dataclasses.fields(cls):
        if field.name.startswith("_"):
            continue
        annotation = hints[field.name]
        path = f"{prefix}.{field.name}" if prefix else field.name
        if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
            paths.update(_dataclass_paths(annotation, path))
            continue
        args = get_args(annotation)
        if get_origin(annotation) is tuple and args and dataclasses.is_dataclass(args[0]):
            paths.update(_dataclass_paths(args[0], f"{path}[]"))
            continue
        paths.add(path)
    return paths


def _schema_paths(root: dict[str, Any], node: dict[str, Any], prefix: str = "") -> set[str]:
    node = _resolve_schema(root, node)
    paths: set[str] = set()
    for name, raw_child in node.get("properties", {}).items():
        path = f"{prefix}.{name}" if prefix else name
        child = _resolve_schema(root, raw_child)
        if child.get("type") == "object" and "properties" in child:
            paths.update(_schema_paths(root, child, path))
            continue
        if child.get("type") == "array" and isinstance(child.get("items"), dict):
            items = _resolve_schema(root, child["items"])
            if items.get("type") == "object" and "properties" in items:
                paths.update(_schema_paths(root, items, f"{path}[]"))
                continue
        paths.add(path)
    return paths


def _resolve_schema(root: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    if "$ref" not in node:
        return node
    prefix = "#/$defs/"
    reference = node["$ref"]
    if not reference.startswith(prefix):
        raise AssertionError(f"Unsupported schema reference: {reference}")
    return root["$defs"][reference.removeprefix(prefix)]


if __name__ == "__main__":
    unittest.main()
