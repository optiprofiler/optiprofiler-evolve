from __future__ import annotations

import unittest

from optiprofiler_evolve.data import DataPlan
from optiprofiler_evolve.prompt import build_worker_prompt
from optiprofiler_evolve.solver import InterfaceSpec


def _prompt_kwargs() -> dict:
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
    return {
        "interface": InterfaceSpec.parse("solver.py:solver"),
        "runtime": "python",
        "editable": ("*.py",),
        "data": data,
        "iteration": 1,
        "island": 0,
        "parent_score": 0.5,
        "controller_memory": None,
        "token_budget": 1000,
        "max_smoke_calls": 2,
        "max_public_calls": 1,
        "forbidden_candidate_imports": (),
    }


class PromptTests(unittest.TestCase):
    def test_prompt_note_renders_only_when_configured(self) -> None:
        kwargs = _prompt_kwargs()
        without_note = build_worker_prompt(**kwargs)
        self.assertNotIn("Experiment note", without_note)

        with_note = build_worker_prompt(
            **kwargs,
            prompt_note="Make one local improvement and stop after two smoke calls.",
        )
        self.assertIn("Experiment note (must follow)", with_note)
        self.assertIn(
            "Make one local improvement and stop after two smoke calls.", with_note
        )
        self.assertIn("does not change", with_note)
        # The note slots between the work order and the delivery reminder.
        self.assertLess(
            with_note.index("Required work order"),
            with_note.index("Experiment note (must follow)"),
        )
        self.assertLess(
            with_note.index("Experiment note (must follow)"),
            with_note.index("Public experiment manifest"),
        )
        # Everything else stays identical.
        self.assertEqual(
            without_note,
            with_note.replace(
                "\nExperiment note (must follow)\n"
                "Make one local improvement and stop after two smoke calls.\n"
                "This note constrains how you spend the work budget. It does not change "
                "the contract above, the evaluation tools, or any isolation rule.\n",
                "",
            ),
        )


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
        self.assertNotIn("s2mpj", prompt)
        self.assertNotIn("fixed reference solver", prompt)
        self.assertNotIn("(candidate - reference + 1) / 2", prompt)


if __name__ == "__main__":
    unittest.main()
