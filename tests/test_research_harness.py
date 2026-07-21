from __future__ import annotations

import difflib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.engine import EvolutionEngine
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.phases.research import RecombinePhase, StrategyAnalysisPhase
from optiprofiler_evolve.protocols import PhaseContext, VariantRequest
from optiprofiler_evolve.sandbox import AgentRunResult
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash

from test_config import minimal_config


class ResearchEvaluator:
    calls: list[tuple[str, str]] = []

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        text = (candidate / "solver.py").read_text(encoding="utf-8")
        if "# improved\n" in text:
            score = 0.7
        else:
            score = 0.5 + 0.1 * sum(
                marker in text for marker in ("# improved-0", "# improved-1")
            )
        if mode == "validation" and score > 0.5:
            score -= 0.01
        if mode == "hidden" and score > 0.5:
            score -= 0.02
        output_dir.mkdir(parents=True, exist_ok=True)
        result = EvaluationResult(mode, score, score, 0.5, 2, output_dir)
        (output_dir / "result.json").write_text(
            json.dumps(result.as_dict(), default=str), encoding="utf-8"
        )
        (output_dir / "feedback.md").write_text("public evidence", encoding="utf-8")
        self.calls.append((candidate.name, mode))
        return result


def research_evaluator_factory(**_kwargs: object) -> ResearchEvaluator:
    return ResearchEvaluator()


class ScriptedResearchRunner:
    prompts: list[str] = []

    def __call__(self, **kwargs: object) -> AgentRunResult:
        workspace = kwargs["workspace"]
        prompt = str(kwargs["prompt"])
        transcript = kwargs["transcript"]
        assert isinstance(workspace, Path)
        assert isinstance(transcript, Path)
        self.prompts.append(prompt)
        if "ROLE: direction-scout" in prompt:
            (workspace / "direction_cards.json").write_text(
                json.dumps(
                    {
                        "cards": [
                            {
                                "card_id": "d1",
                                "title": "adaptive polling",
                                "hypothesis": "Adapt polling to recent progress.",
                                "tactics": ["change polling order"],
                                "citations": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        elif "ROLE: strategy-analyst" in prompt:
            variants = workspace / "variants"
            effective = variants / "effective"
            placebo = variants / "placebo"
            shutil.copytree(workspace / "finalist", effective)
            shutil.copytree(workspace / "finalist", placebo)
            effective_solver = effective / "solver.py"
            effective_solver.write_text(
                effective_solver.read_text(encoding="utf-8").replace("# improved\n", ""),
                encoding="utf-8",
            )
            placebo_solver = placebo / "solver.py"
            placebo_solver.write_text(
                placebo_solver.read_text(encoding="utf-8").replace("# inert\n", ""),
                encoding="utf-8",
            )
            island = 0 if "island 0" in prompt else 1
            (workspace / "strategy_cards.json").write_text(
                json.dumps(
                    {
                        "cards": [
                            {
                                "strategy_id": f"i{island}-effective",
                                "claim": "The improvement marker represents useful logic.",
                                "code_bindings": [{"file": "solver.py", "lines": [1, 5]}],
                                "toggle": {
                                    "kind": "variant_tree",
                                    "ref": "variants/effective",
                                },
                            },
                            {
                                "strategy_id": f"i{island}-placebo",
                                "claim": "The inert marker is useful.",
                                "code_bindings": [{"file": "solver.py", "lines": [1, 5]}],
                                "toggle": {
                                    "kind": "variant_tree",
                                    "ref": "variants/placebo",
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
        else:
            solver = workspace / "solver.py"
            solver.write_text(
                solver.read_text(encoding="utf-8") + "\n# improved\n# inert\n",
                encoding="utf-8",
            )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("scripted worker", encoding="utf-8")
        return AgentRunResult(0, transcript)


def _write_solver_patch(path: Path, before: str, after: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="a/solver.py",
                tofile="b/solver.py",
            )
        ),
        encoding="utf-8",
    )


class FullResearchPathRunner:
    """Exercise pruning, portable recombination, and champion registration."""

    def __call__(self, **kwargs: object) -> AgentRunResult:
        workspace = kwargs["workspace"]
        prompt = str(kwargs["prompt"])
        transcript = kwargs["transcript"]
        assert isinstance(workspace, Path)
        assert isinstance(transcript, Path)
        if "ROLE: direction-scout" in prompt:
            (workspace / "direction_cards.json").write_text(
                json.dumps(
                    {
                        "cards": [
                            {
                                "card_id": "d1",
                                "title": "independent mechanisms",
                                "hypothesis": "Test separate solver mechanisms.",
                            }
                        ]
                    }
                )
            )
        elif "ROLE: strategy-analyst" in prompt:
            island = 0 if "island 0" in prompt else 1
            finalist_text = (workspace / "finalist" / "solver.py").read_text()
            effective = workspace / "toggles" / "effective.patch"
            placebo = workspace / "toggles" / "placebo.patch"
            portable = workspace / "portable" / "effective.patch"
            _write_solver_patch(
                effective,
                finalist_text,
                finalist_text.replace(f"# improved-{island}", f"# strategy-{island}-slot"),
            )
            _write_solver_patch(
                placebo,
                finalist_text,
                finalist_text.replace(f"# inert-{island}", f"# inert-{island}-slot"),
            )
            portable.parent.mkdir(parents=True, exist_ok=True)
            if island == 0:
                portable_patch = (
                    "--- a/solver.py\n"
                    "+++ b/solver.py\n"
                    "@@ -2,2 +2,2 @@\n"
                    "     return x0\n"
                    "-# strategy-0-slot\n"
                    "+# improved-0\n"
                )
            else:
                portable_patch = (
                    "--- a/solver.py\n"
                    "+++ b/solver.py\n"
                    "@@ -4,2 +4,2 @@\n"
                    "-# strategy-1-slot\n"
                    "+# improved-1\n"
                    " # inert-0-slot\n"
                )
            portable.write_text(portable_patch, encoding="utf-8")
            (workspace / "strategy_cards.json").write_text(
                json.dumps(
                    {
                        "cards": [
                            {
                                "strategy_id": f"i{island}-effective",
                                "claim": "The island-specific mechanism improves fitness.",
                                "code_bindings": [{"file": "solver.py", "lines": [3, 6]}],
                                "toggle": {
                                    "kind": "removal_patch",
                                    "ref": "toggles/effective.patch",
                                },
                                "portable_patch": {
                                    "ref": "portable/effective.patch",
                                    "base": "seed",
                                },
                            },
                            {
                                "strategy_id": f"i{island}-placebo",
                                "claim": "The inert marker does not improve fitness.",
                                "code_bindings": [{"file": "solver.py", "lines": [3, 6]}],
                                "toggle": {
                                    "kind": "removal_patch",
                                    "ref": "toggles/placebo.patch",
                                },
                            },
                        ]
                    }
                )
            )
        else:
            island = 0 if "island: 0" in prompt else 1
            solver = workspace / "solver.py"
            text = solver.read_text()
            text = text.replace(f"# strategy-{island}-slot", f"# improved-{island}")
            text = text.replace(f"# inert-{island}-slot", f"# inert-{island}")
            solver.write_text(text)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("scripted full research path", encoding="utf-8")
        return AgentRunResult(0, transcript)


def research_config() -> dict:
    raw = minimal_config()
    raw["workflow"] = {
        "phases": [
            {"name": "prepare"},
            {
                "name": "direction_scout",
                "options": {
                    "mode": "shared",
                    "guided_islands": [0],
                    "tools": {"network": False, "web_search": False},
                },
            },
            {"name": "explore"},
            {
                "name": "strategy_analysis",
                "options": {
                    "max_strategies": 2,
                    "max_ablations": 2,
                    "tools": {"network": False, "web_search": False},
                },
            },
            {"name": "recombine"},
            {"name": "validate"},
            {"name": "hidden"},
            {"name": "challenger", "options": {"reference": "scipy_powell"}},
            {"name": "report"},
        ]
    }
    return raw


class ResearchHarnessTests(unittest.TestCase):
    def test_strategy_analyst_keeps_model_transport_but_disables_web_search(self) -> None:
        phase = StrategyAnalysisPhase()

        self.assertTrue(phase.tools.network)
        self.assertFalse(phase.tools.web_search)

    def test_scripted_full_workflow_detects_effective_and_placebo_strategies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ResearchEvaluator.calls = []
            runner = ScriptedResearchRunner()
            runner.prompts = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            source_hash = tree_hash(source)
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(research_config()),
                run_dir=root / "run",
                agent_runner=runner,
                evaluator_factory=research_evaluator_factory,
            )
            result = engine.run()

            self.assertEqual(tree_hash(source), source_hash)
            self.assertEqual(result.public_score, 0.7)
            self.assertEqual(sum(mode == "hidden" for _name, mode in ResearchEvaluator.calls), 1)
            directions = json.loads(
                (root / "run" / "research" / "directions.json").read_text(encoding="utf-8")
            )
            self.assertEqual(directions["assignment"], {"0": "d1", "1": None})
            mutation_prompts = [
                prompt
                for prompt in runner.prompts
                if "ROLE:" not in prompt and "You are improving" in prompt
            ]
            self.assertTrue(
                any("island: 0" in prompt and "adaptive polling" in prompt for prompt in mutation_prompts)
            )
            self.assertTrue(
                any(
                    "island: 1" in prompt and "No scout direction is assigned" in prompt
                    for prompt in mutation_prompts
                )
            )

            for island in (0, 1):
                cards = json.loads(
                    (
                        root
                        / "run"
                        / "research"
                        / "analysis"
                        / f"island-{island}"
                        / "strategy_cards.json"
                    ).read_text(encoding="utf-8")
                )["cards"]
                conclusions = {
                    card["strategy_id"].split("-")[-1]: card["ablation"]["conclusion"]
                    for card in cards
                }
                self.assertEqual(conclusions, {"effective": "supported", "placebo": "placebo"})
                self.assertTrue(all(card["evidence_level"] == "Observed" for card in cards))
            bundles = json.loads(
                (
                    root / "run" / "research" / "analysis" / "island_bundles.json"
                ).read_text(encoding="utf-8")
            )["bundles"]
            self.assertTrue(
                all("effective" in item["supported_strategy_ids"][0] for item in bundles)
            )
            self.assertTrue(
                all("placebo" in item["unprunable_strategy_ids"][0] for item in bundles)
            )
            self.assertTrue(all(item["dropped_strategy_ids"] == [] for item in bundles))
            recombination = json.loads(
                (root / "run" / "research" / "recombination.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recombination["status"], "skipped")
            challenger = json.loads(
                (
                    root / "run" / "research" / "challenger_report.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(challenger["selection_effect"], "none")
            self.assertIn("not numerically interchangeable", challenger["comparability_note"])
            validation_usage = json.loads(
                (root / "run" / "research" / "validation_usage.json").read_text()
            )
            self.assertEqual(validation_usage["selection_calls"], 3)
            self.assertEqual(validation_usage["new_evaluations"], 0)

            for role_workspace in (root / "run" / "research" / "roles").glob("*/*"):
                names = {path.name for path in role_workspace.rglob("*")}
                self.assertNotIn("evaluate", names)
                self.assertNotIn("smoke_test", names)
                for path in role_workspace.rglob("*"):
                    if path.is_file() and path.suffix in {".json", ".jsonl", ".md", ".txt"}:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        self.assertNotIn(str(root / "run" / "controller"), text)

    def test_full_research_path_registers_pruned_and_recombined_finalists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ResearchEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text(
                "def solver(fun, x0):\n"
                "    return x0\n"
                "# strategy-0-slot\n"
                "# strategy-1-slot\n"
                "# inert-0-slot\n"
                "# inert-1-slot\n"
            )
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(research_config()),
                run_dir=root / "run",
                agent_runner=FullResearchPathRunner(),
                evaluator_factory=research_evaluator_factory,
            )

            result = engine.run()

            bundles = json.loads(
                (root / "run" / "research" / "analysis" / "island_bundles.json").read_text()
            )["bundles"]
            self.assertTrue(
                all(item["materialization"] == "pruned_removal_patches" for item in bundles)
            )
            self.assertEqual(
                {item["candidate_id"] for item in bundles},
                {"bundle-island-0", "bundle-island-1"},
            )
            self.assertTrue(
                all(item["dropped_strategy_ids"] == [f"i{item['island']}-placebo"] for item in bundles)
            )
            recombination = json.loads(
                (root / "run" / "research" / "recombination.json").read_text()
            )
            self.assertEqual(recombination["status"], "ok", recombination)
            self.assertEqual(
                [item["status"] for item in recombination["combinations"]],
                ["succeeded"],
            )
            self.assertIn("combo-001", engine.research_finalists)
            self.assertTrue(
                {"bundle-island-0", "bundle-island-1"}.issubset(engine.research_finalists)
            )
            selection = json.loads((root / "run" / "validation_selection.json").read_text())
            self.assertIn("combo-001", selection["finalists"])
            self.assertEqual(result.best_candidate_id, "combo-001")
            self.assertAlmostEqual(result.public_score, 0.7)
            self.assertEqual(sum(mode == "hidden" for _name, mode in ResearchEvaluator.calls), 1)
            usage = json.loads((root / "run" / "research" / "validation_usage.json").read_text())
            self.assertGreaterEqual(usage["selection_calls"], 6)
            self.assertEqual(usage["new_evaluations"], 3)

    def test_variant_gate_rejects_out_of_scope_and_conflicting_patches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            (source / "locked.txt").write_text("original\n")
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                agent_runner=ScriptedResearchRunner(),
                evaluator_factory=research_evaluator_factory,
            )
            patch_root = root / "run" / "research" / "test-patches"
            patch_root.mkdir(parents=True)
            locked_patch = patch_root / "locked.patch"
            locked_patch.write_text(
                "--- a/locked.txt\n+++ b/locked.txt\n@@ -1 +1 @@\n-original\n+changed\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "outside editable scope"):
                engine.controller_services.materialize_variant(
                    VariantRequest("locked-change", source, patches=(locked_patch,))
                )
            self.assertFalse((root / "run" / "research" / "variants" / "locked-change").exists())

            first = patch_root / "first.patch"
            second = patch_root / "second.patch"
            first.write_text(
                "--- a/solver.py\n+++ b/solver.py\n@@ -1,2 +1,2 @@\n def solver(fun, x0):\n-    return x0\n+    return x0 + 1\n",
                encoding="utf-8",
            )
            second.write_text(
                "--- a/solver.py\n+++ b/solver.py\n@@ -1,2 +1,2 @@\n def solver(fun, x0):\n-    return x0\n+    return x0 + 2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Patch conflict"):
                engine.controller_services.materialize_variant(
                    VariantRequest("conflict", source, patches=(first, second))
                )
            self.assertFalse((root / "run" / "research" / "variants" / "conflict").exists())

    def test_scout_and_analyst_failures_fall_back_without_stopping_search(self) -> None:
        def failing_roles(**kwargs: object) -> AgentRunResult:
            workspace = kwargs["workspace"]
            prompt = str(kwargs["prompt"])
            transcript = kwargs["transcript"]
            assert isinstance(workspace, Path)
            assert isinstance(transcript, Path)
            if "ROLE:" not in prompt:
                solver = workspace / "solver.py"
                solver.write_text(solver.read_text() + "\n# improved\n# inert\n")
                code = 0
            else:
                if "ROLE: direction-scout" in prompt:
                    (workspace / "direction_cards.json").write_text(
                        json.dumps(
                            {
                                "cards": [
                                    {
                                        "card_id": "must-not-be-accepted",
                                        "title": "invalid failed output",
                                        "hypothesis": "A failed role must not publish artifacts.",
                                    }
                                ]
                            }
                        )
                    )
                code = 1
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("scripted failure", encoding="utf-8")
            return AgentRunResult(code, transcript)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            result = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(research_config()),
                run_dir=root / "run",
                agent_runner=failing_roles,
                evaluator_factory=research_evaluator_factory,
            ).run()
            self.assertEqual(result.public_score, 0.7)
            directions = json.loads(
                (root / "run" / "research" / "directions.json").read_text()
            )
            self.assertEqual(directions["status"], "off-fallback")
            self.assertEqual(directions["cards"], [])
            bundles = json.loads(
                (root / "run" / "research" / "analysis" / "island_bundles.json").read_text()
            )["bundles"]
            self.assertTrue(all(item["materialization"] == "original_finalist" for item in bundles))

    def test_recombination_records_conflict_instead_of_force_merging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                agent_runner=ScriptedResearchRunner(),
                evaluator_factory=research_evaluator_factory,
            )
            engine._initialize_run_directory()
            prepared = engine._phase_prepare()["prepared"]
            seed = Path(str(prepared["seed"]))
            portable = root / "run" / "research" / "analysis" / "island-1" / "portable"
            portable.mkdir(parents=True)
            first = portable / "s1.patch"
            second = portable / "s2.patch"
            first.write_text(
                "--- a/solver.py\n+++ b/solver.py\n@@ -1,2 +1,2 @@\n def solver(fun, x0):\n-    return x0\n+    return x0 + 1\n",
                encoding="utf-8",
            )
            second.write_text(
                "--- a/solver.py\n+++ b/solver.py\n@@ -1,2 +1,2 @@\n def solver(fun, x0):\n-    return x0\n+    return x0 + 2\n",
                encoding="utf-8",
            )
            bundles = root / "run" / "research" / "analysis" / "island_bundles.json"
            bundles.write_text(
                json.dumps(
                    {
                        "bundles": [
                            {
                                "island": 0,
                                "candidate_id": "seed",
                                "candidate_path": str(seed),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            cards = portable.parent / "strategy_cards.json"
            cards.write_text(
                json.dumps(
                    {
                        "island": 1,
                        "cards": [
                            {
                                "strategy_id": "i1-s1",
                                "ablation": {"conclusion": "supported"},
                                "portable_patch": {
                                    "ref": "portable/s1.patch",
                                    "base": "seed",
                                    "base_tree_hash": tree_hash(seed),
                                },
                            },
                            {
                                "strategy_id": "i1-s2",
                                "ablation": {"conclusion": "supported"},
                                "portable_patch": {
                                    "ref": "portable/s2.patch",
                                    "base": "seed",
                                    "base_tree_hash": tree_hash(seed),
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            phase = RecombinePhase(max_combination_size=2, max_combinations=3, beam_width=1)
            result = phase.run(
                PhaseContext(
                    run_dir=root / "run",
                    options={},
                    artifacts={
                        "prepared": prepared,
                        "island_bundles": bundles,
                        "strategy_cards": (cards,),
                    },
                    invoke=lambda _name: {},
                    emit=lambda _kind, _status, _data=None: None,
                    services=engine.controller_services,
                )
            )
            payload = json.loads(Path(str(result.artifacts["combinations"])).read_text())
            statuses = [item["status"] for item in payload["combinations"]]
            self.assertEqual(statuses.count("succeeded"), 2)
            self.assertEqual(statuses.count("patch_conflict_or_gate_rejected"), 1)
            self.assertFalse((root / "run" / "research" / "variants" / "combo-003").exists())

    def test_recombination_ignores_islands_without_a_valid_finalist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                agent_runner=ScriptedResearchRunner(),
                evaluator_factory=research_evaluator_factory,
            )
            engine._initialize_run_directory()
            prepared = engine._phase_prepare()["prepared"]
            seed = Path(str(prepared["seed"]))
            bundles = root / "run" / "research" / "analysis" / "island_bundles.json"
            bundles.parent.mkdir(parents=True)
            bundles.write_text(
                json.dumps(
                    {
                        "bundles": [
                            {
                                "island": 0,
                                "status": "skipped",
                                "reason": "no valid finalist",
                            },
                            {
                                "island": 1,
                                "candidate_id": "seed",
                                "candidate_path": str(seed),
                            },
                        ]
                    }
                )
            )
            cards = root / "run" / "research" / "analysis" / "island-1" / "strategy_cards.json"
            cards.parent.mkdir(parents=True)
            cards.write_text(json.dumps({"island": 1, "cards": []}))

            result = RecombinePhase().run(
                PhaseContext(
                    run_dir=root / "run",
                    options={},
                    artifacts={
                        "prepared": prepared,
                        "island_bundles": bundles,
                        "strategy_cards": (cards,),
                    },
                    invoke=lambda _name: {},
                    emit=lambda _kind, _status, _data=None: None,
                    services=engine.controller_services,
                )
            )

            payload = json.loads(Path(str(result.artifacts["combinations"])).read_text())
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["base_candidate"], "seed")


if __name__ == "__main__":
    unittest.main()
