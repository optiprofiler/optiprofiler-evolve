from __future__ import annotations

import unittest

from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.prompt import build_worker_prompt
from optiprofiler_evolve.solver import InterfaceSpec


class PromptTests(unittest.TestCase):
    def test_evaluation_quotas_are_explicit_and_non_retryable(self) -> None:
        data = DataPlan(
            library="s2mpj",
            selection={},
            universe=("P1",),
            public=("P1",),
            validation=(),
            hidden=(),
            smoke=("P1",),
            split_seed=1,
            manifest_hash="test",
            aliases={"P1": "P_OPAQUE"},
        )
        prompt = build_worker_prompt(
            interface=InterfaceSpec.parse("solver.py:solver"),
            runtime="python",
            editable=("*.py",),
            data=data,
            iteration=1,
            island=0,
            parent_score=0.5,
            controller_memory=None,
            token_budget=1000,
            max_smoke_calls=3,
            max_public_calls=1,
            forbidden_candidate_imports=("scipy.optimize",),
        )

        self.assertIn("at most\n  3 calls", prompt)
        self.assertIn("at most\n  1 calls", prompt)
        self.assertIn("waiting cannot restore it", prompt)
        self.assertIn("do not sleep or retry", prompt)
        self.assertIn("render_pdf", prompt)
        self.assertIn("scipy.optimize", prompt)


if __name__ == "__main__":
    unittest.main()
