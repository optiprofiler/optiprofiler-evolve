from __future__ import annotations

import unittest
from dataclasses import fields
from unittest.mock import patch

from optiprofiler_evolve.config import AssessmentConfig, CriterionConfig
from optiprofiler_evolve.controller.builtins import register_builtin_components
from optiprofiler_evolve.controller.registry import register
from optiprofiler_evolve.criteria.contracts import Criterion, GuidanceRequest
from optiprofiler_evolve.criteria.guidance import build_generation_guidance


class GuidanceOnlyCriterion(Criterion):
    guidance_fields = ("public_rule",)
    requests = []

    def __init__(self):
        raise AssertionError("Guidance must not allocate an evaluator or reviewer")

    def assess(self, request):
        raise AssertionError("Guidance must not execute a measurement")

    @classmethod
    def generation_guidance(cls, request):
        cls.requests.append(request)
        return str(dict(request.options))


class CriterionGuidanceTests(unittest.TestCase):
    def setUp(self):
        register_builtin_components()
        register("criterion", "guidance_test", GuidanceOnlyCriterion, replace=True)
        GuidanceOnlyCriterion.requests = []

    def test_each_standard_owns_its_instruction_and_can_be_disabled(self):
        markers = {"performance": "Performance:", "complexity": "Complexity budget",
                   "integrity": "Semantic integrity:"}
        for enabled, marker in markers.items():
            with self.subTest(criterion=enabled):
                config = AssessmentConfig(criteria={
                    name: CriterionConfig(enabled=name == enabled) for name in markers
                })
                prompt = build_generation_guidance(config, "full_artifacts")
                self.assertIn(marker, prompt)
                for other, absent in markers.items():
                    if other != enabled:
                        self.assertNotIn(absent, prompt)
                if enabled != "performance":
                    self.assertNotIn("render_pdf", prompt)
                    self.assertNotIn("history plots", prompt)

    def test_performance_artifact_instructions_follow_available_public_feedback(self):
        config = AssessmentConfig(criteria={"performance": CriterionConfig()})
        for mode in ("score_only", "full_artifacts"):
            with self.subTest(mode=mode):
                prompt = build_generation_guidance(config, mode)
                self.assertIn("history plots", prompt)
                self.assertIn("render_pdf", prompt)
        formal_only = AssessmentConfig(criteria=config.criteria, profiles={
            "public": {"performance": {"enabled": False}},
            "smoke": {"performance": {"enabled": False}},
        })
        prompt = build_generation_guidance(formal_only, "full_artifacts")
        self.assertIn("Performance:", prompt)
        self.assertNotIn("history plots", prompt)
        self.assertNotIn("render_pdf", prompt)
        self.assertNotIn("returned by the enabled public checks", prompt)

    def test_profile_rules_are_distinct_but_identical_guidance_is_not_repeated(self):
        config = AssessmentConfig(criteria={"complexity": CriterionConfig(options={"max_ratio": 1.7})},
                                  profiles={"validation": {"complexity": {
                                      "weight": 3, "mode": "warn", "options": {"max_ratio": 1.1},
                                  }}, "hidden": {"complexity": {"options": {"max_ratio": 987654}}}})
        prompt = build_generation_guidance(config, "score_only")
        self.assertEqual(prompt.count("Complexity budget"), 2)
        self.assertIn("smoke, public; weight=1; enforce", prompt)
        self.assertIn("validation; weight=3; warn", prompt)
        self.assertIn("1.7 times", prompt)
        self.assertIn("1.1 times", prompt)
        self.assertNotIn("987654", prompt)

    def test_custom_guidance_receives_only_allowlisted_rules_without_instantiation(self):
        config = AssessmentConfig(criteria={"guidance_test": CriterionConfig(options={
            "public_rule": "prefer fewer operations", "api_key": "SECRET_CANARY",
            "problem_names": ["PRIVATE_PROBLEM"], "validation_result": "PRIVATE_RESULT",
        })}, profiles={"hidden": {"guidance_test": {"options": {"public_rule": "HIDDEN_CANARY"}}}})
        prompt = build_generation_guidance(config, "score_only")
        self.assertIn("prefer fewer operations", prompt)
        self.assertEqual(len(GuidanceOnlyCriterion.requests), 1)
        request = GuidanceOnlyCriterion.requests[0]
        self.assertEqual(dict(request.options), {"public_rule": "prefer fewer operations"})
        with self.assertRaises(TypeError):
            request.options["public_rule"] = "changed"
        self.assertEqual({field.name for field in fields(request)},
                         {"profiles", "options", "weight", "mode", "feedback_mode"})
        for private in ("SECRET_CANARY", "PRIVATE_PROBLEM", "PRIVATE_RESULT", "HIDDEN_CANARY"):
            self.assertNotIn(private, prompt)

    def test_disabled_and_hidden_only_standards_do_not_resolve_or_generate_prompts(self):
        config = AssessmentConfig(criteria={
            "guidance_test": CriterionConfig(enabled=False),
            "hidden_only_unregistered": CriterionConfig(),
        }, profiles={p: {"hidden_only_unregistered": {"enabled": False}}
                     for p in ("smoke", "public", "validation")})
        with patch("optiprofiler_evolve.criteria.guidance.resolve") as resolve:
            build_generation_guidance(config, "score_only")
        resolve.assert_not_called()
        self.assertFalse(GuidanceOnlyCriterion.requests)

    def test_reviewer_settings_are_not_generation_inputs(self):
        config = AssessmentConfig(criteria={"integrity": CriterionConfig(options={
            "worker": {"model": "PRIVATE_MODEL", "env": {"API_KEY": "SECRET_CANARY"}},
            "component": {"name": "PRIVATE_COMPONENT"},
        })})
        prompt = build_generation_guidance(config, "score_only")
        self.assertIn("Semantic integrity:", prompt)
        for private in ("PRIVATE_MODEL", "SECRET_CANARY", "PRIVATE_COMPONENT"):
            self.assertNotIn(private, prompt)

    def test_guidance_context_rejects_hidden_profile(self):
        with self.assertRaises(ValueError):
            GuidanceRequest(("hidden",), {}, 1, "enforce", "score_only")
