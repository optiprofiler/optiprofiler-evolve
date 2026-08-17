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
        "integrity_review": {
            "component": {"name": "unsafe_approve"},
            "allow_unsafe_stub": True,
        },
        "sandbox": {"backend": "unsafe_local"},
    }


class ConfigTests(unittest.TestCase):

    def test_prompt_note_is_validated(self) -> None:
        raw = minimal_config()
        raw["workers"]["prompt_note"] = "One local change, two smoke calls, then finish."
        config = load_config(raw)
        self.assertEqual(
            config.workers.prompt_note,
            "One local change, two smoke calls, then finish.",
        )

        raw["workers"]["prompt_note"] = "   "
        with self.assertRaisesRegex(ValueError, "prompt_note"):
            load_config(raw)

        raw["workers"]["prompt_note"] = "x" * 2001
        with self.assertRaisesRegex(ValueError, "2000"):
            load_config(raw)

        raw["workers"]["prompt_note"] = 7
        with self.assertRaises((TypeError, ValueError)):
            load_config(raw)

    def test_nested_config_is_strict_and_typed(self) -> None:
        config = load_config(minimal_config())
        self.assertEqual(config.evolution.islands, 2)
        self.assertEqual(config.workers.pool[0].harness, "codex")
        self.assertEqual(config.data.problem_names, ("P1", "P2", "P3", "P4"))
        self.assertEqual(config.evolution.retention.name, "validation_lexicographic")
        self.assertEqual(
            config.evolution.parent_sampler.name,
            "top_biased_validation_weighted",
        )
        self.assertEqual(config.evolution.parent_sampler.options["greedy_ratio"], 0.7)
        self.assertEqual(config.integrity_review.component.name, "unsafe_approve")

    def test_agent_reviewer_requires_a_distinct_model_or_explicit_override(self) -> None:
        raw = minimal_config()
        raw["integrity_review"] = {"component": {"name": "agent_integrity"}}
        with self.assertRaisesRegex(ValueError, "dedicated integrity_review.worker"):
            load_config(raw)

        raw["integrity_review"]["allow_same_model"] = True
        config = load_config(raw)
        self.assertEqual(
            config.integrity_review.resolved_worker(config.workers).model,
            "test-model",
        )

    def test_unsafe_reviewer_requires_double_opt_in(self) -> None:
        raw = minimal_config()
        raw["integrity_review"].pop("allow_unsafe_stub")
        with self.assertRaisesRegex(ValueError, "allow_unsafe_stub=true"):
            load_config(raw)

    def test_dedicated_reviewer_must_differ_and_its_secrets_are_redacted(self) -> None:
        raw = minimal_config()
        raw["integrity_review"] = {
            "component": {"name": "agent_integrity"},
            "worker": {
                "harness": "claude",
                "model": "review-model",
                "env": {"ANTHROPIC_API_KEY": "review-secret"},
            },
        }
        config = load_config(raw)
        self.assertEqual(config.integrity_review.worker.model, "review-model")
        self.assertEqual(
            config.redacted_dict()["integrity_review"]["worker"]["env"][
                "ANTHROPIC_API_KEY"
            ],
            "<redacted>",
        )
        raw["integrity_review"]["worker"]["harness"] = "codex"
        raw["integrity_review"]["worker"]["model"] = "test-model"
        with self.assertRaisesRegex(ValueError, "outside the mutation pool"):
            load_config(raw)

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

    def test_profile_tensor_fitness_source_is_strict(self) -> None:
        raw = minimal_config()
        raw["evaluation"]["fitness_source"] = "profile_scores_mean"
        config = load_config(raw)
        self.assertEqual(config.evaluation.fitness_source, "profile_scores_mean")

        raw["evaluation"]["fitness_source"] = "unknown"
        with self.assertRaisesRegex(ValueError, "fitness_source"):
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

    def test_docker_provider_transport_is_gateway_first_and_direct_is_double_opt_in(self) -> None:
        raw = minimal_config()
        raw["evaluation"] = {
            "backend": "docker",
            "docker_image": "evaluator:test",
        }
        raw["sandbox"] = {"backend": "docker", "worker_image": "worker:test"}
        with self.assertRaisesRegex(ValueError, "both workers.allow_direct_provider"):
            load_config(raw)

        raw["workers"]["allow_direct_provider"] = True
        with self.assertRaisesRegex(ValueError, "both workers.allow_direct_provider"):
            load_config(raw)

        raw["sandbox"]["allow_direct_network"] = True
        config = load_config(raw)
        self.assertTrue(config.workers.allow_direct_provider)
        self.assertTrue(config.sandbox.allow_direct_network)

    def test_gateway_route_is_strict_and_credential_value_is_redacted(self) -> None:
        raw = minimal_config()
        raw["evaluation"] = {
            "backend": "docker",
            "docker_image": "evaluator:test",
        }
        raw["sandbox"] = {"backend": "docker", "worker_image": "worker:test"}
        raw["workers"]["pool"][0].update(
            {
                "env": {"OPENAI_API_KEY": "secret"},
                "provider_gateway": {
                    "upstream_base_url": "https://api.example/v1",
                    "credential_env": "OPENAI_API_KEY",
                },
            }
        )
        config = load_config(raw)
        gateway = config.workers.pool[0].provider_gateway
        assert gateway is not None
        self.assertEqual(gateway.resolved_protocol("codex"), "openai_responses")
        self.assertEqual(
            config.redacted_dict()["workers"]["pool"][0]["env"]["OPENAI_API_KEY"],
            "<redacted>",
        )

        raw["workers"]["pool"][0]["provider_gateway"]["upstream_base_url"] = (
            "http://metadata.internal"
        )
        with self.assertRaisesRegex(ValueError, "https origin"):
            load_config(raw)

    def test_gateway_cannot_be_claimed_by_unsafe_local_or_overridden_by_codex_args(self) -> None:
        raw = minimal_config()
        raw["workers"]["pool"][0].update(
            {
                "env": {"OPENAI_API_KEY": "secret"},
                "provider_gateway": {
                    "upstream_base_url": "https://api.example/v1",
                    "credential_env": "OPENAI_API_KEY",
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "requires the Docker sandbox"):
            load_config(raw)

        raw["workers"]["pool"][0]["args"] = ['model_provider="worker_override"']
        with self.assertRaisesRegex(ValueError, "controller-owned"):
            load_config(raw)

    def test_custom_worker_adapter_cannot_claim_the_builtin_gateway(self) -> None:
        raw = minimal_config()
        raw["evaluation"] = {
            "backend": "docker",
            "docker_image": "evaluator:test",
        }
        raw["sandbox"] = {"backend": "docker", "worker_image": "worker:test"}
        raw["workers"]["adapter"] = "custom"
        raw["workers"]["pool"][0].update(
            {
                "env": {"OPENAI_API_KEY": "secret"},
                "provider_gateway": {
                    "upstream_base_url": "https://api.example/v1",
                    "credential_env": "OPENAI_API_KEY",
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "requires workers.adapter='cli'"):
            load_config(raw)


if __name__ == "__main__":
    unittest.main()
