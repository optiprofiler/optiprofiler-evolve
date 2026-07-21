from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from optiprofiler_evolve.config import load_config


def minimal_config() -> dict:
    return {
        "data": {
            "problem_names": ["P1", "P2", "P3", "P4"],
            "split": {"smoke_count": 1, "hidden_fraction": 0.25},
        },
        "evaluation": {"backend": "unsafe_local"},
        "evolution": {"rounds": 1, "islands": 2},
        "workers": {
            "pool": [{"harness": "codex", "model": "test-model"}],
            "max_parallel": 2,
        },
        "sandbox": {"backend": "unsafe_local"},
    }


class ConfigTests(unittest.TestCase):
    def test_nested_config_is_strict_and_typed(self) -> None:
        config = load_config(minimal_config())
        self.assertEqual(config.evolution.islands, 2)
        self.assertEqual(config.workers.pool[0].harness, "codex")
        self.assertEqual(config.data.problem_names, ("P1", "P2", "P3", "P4"))

    def test_unknown_key_is_rejected(self) -> None:
        raw = minimal_config()
        raw["workers"]["mystery"] = True
        with self.assertRaisesRegex(ValueError, "Unknown keys"):
            load_config(raw)

    def test_evaluator_cannot_reuse_previous_benchmark_results(self) -> None:
        raw = minimal_config()
        raw["evaluation"]["benchmark"] = {"load": "old-run"}
        with self.assertRaisesRegex(ValueError, "cannot load previous"):
            load_config(raw)

    def test_forbidden_candidate_imports_are_typed_and_validated(self) -> None:
        raw = minimal_config()
        raw["evaluation"]["forbidden_candidate_imports"] = ["scipy.optimize", "pdfo"]
        config = load_config(raw)
        self.assertEqual(
            config.evaluation.forbidden_candidate_imports,
            ("scipy.optimize", "pdfo"),
        )
        raw["evaluation"]["forbidden_candidate_imports"] = ["not valid"]
        with self.assertRaisesRegex(ValueError, "dotted Python module names"):
            load_config(raw)

    def test_codex_no_shell_configuration_fails_closed(self) -> None:
        raw = minimal_config()
        raw["workers"]["tools"] = {"shell": False, "web_search": False}
        with self.assertRaisesRegex(ValueError, "not enforceable by the built-in Codex"):
            load_config(raw)

    def test_environment_reference_is_resolved_then_redacted(self) -> None:
        raw = minimal_config()
        raw["workers"]["pool"][0]["env"] = {
            "OPENAI_API_KEY": "${TEST_OPE_KEY}",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
        }
        with patch.dict(os.environ, {"TEST_OPE_KEY": "secret"}):
            config = load_config(raw)
        self.assertEqual(config.workers.pool[0].env["OPENAI_API_KEY"], "secret")
        self.assertEqual(
            config.redacted_dict()["workers"]["pool"][0]["env"]["OPENAI_API_KEY"],
            "<redacted>",
        )
        self.assertEqual(
            config.redacted_dict()["workers"]["pool"][0]["env"]["OPENAI_BASE_URL"],
            "https://example.invalid/v1",
        )

    def test_environment_reference_can_expand_inside_cli_argument(self) -> None:
        raw = minimal_config()
        raw["workers"]["pool"][0]["args"] = [
            'model_providers.compatible.base_url="${TEST_OPE_BASE_URL}"'
        ]
        with patch.dict(os.environ, {"TEST_OPE_BASE_URL": "https://example.invalid/v1"}):
            config = load_config(raw)
        self.assertEqual(
            config.workers.pool[0].args,
            ('model_providers.compatible.base_url="https://example.invalid/v1"',),
        )


if __name__ == "__main__":
    unittest.main()
