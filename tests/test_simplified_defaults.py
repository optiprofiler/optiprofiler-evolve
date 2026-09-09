from __future__ import annotations

import inspect
import unittest

from optiprofiler_evolve import evolve
from optiprofiler_evolve.shared.config import EvolutionConfig, ToolConfig, WorkflowConfig
from optiprofiler_evolve.shared.presets import dfo_default


class SimplifiedDefaultTests(unittest.TestCase):
    def test_public_evolve_config_is_optional(self) -> None:
        self.assertIsNone(inspect.signature(evolve).parameters["config"].default)

    def test_default_pipeline_contains_only_work_that_changes_or_scores_a_candidate(self) -> None:
        workflow = WorkflowConfig()
        self.assertEqual(
            [step.name for step in workflow.attempt_steps],
            ["mutate", "smoke", "public_evaluate"],
        )
        self.assertEqual(workflow.after_iteration, ())

    def test_whole_candidate_migration_is_opt_in(self) -> None:
        evolution = EvolutionConfig()
        self.assertEqual(evolution.iterations, 1)
        self.assertEqual(evolution.islands, 1)
        self.assertEqual(evolution.population_per_island, 2)
        self.assertEqual(evolution.migration_interval, 0)

    def test_default_tools_are_lean_but_keep_local_algorithm_work_available(self) -> None:
        tools = ToolConfig()
        self.assertEqual(tools.preset, "minimal")
        self.assertFalse(tools.web_search)
        self.assertFalse(tools.compilers)
        self.assertTrue(tools.shell)
        self.assertTrue(tools.python)

    def test_dfo_default_is_a_valid_bounded_quick_start(self) -> None:
        config = dfo_default()
        config.validate()
        self.assertEqual(config.evolution.iterations, 1)
        self.assertEqual(config.evolution.islands, 1)
        self.assertEqual(config.data.selection["ptype"], "u")
        self.assertLessEqual(config.data.selection["maxdim"], 5)


if __name__ == "__main__":
    unittest.main()
