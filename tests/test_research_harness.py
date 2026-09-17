from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_config import minimal_config
from worker_helpers import RunnerAdapter

from optiprofiler_evolve.agents.contracts import WorkerOutcome
from optiprofiler_evolve.application.run import EvolutionRun
from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.controller.contracts import VariantRequest
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.phases.contracts import PhaseContext
from optiprofiler_evolve.phases.prepare import PreparePhase
from optiprofiler_evolve.phases.recombine import RecombinePhase
from optiprofiler_evolve.phases.strategy_analysis import StrategyAnalysisPhase
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash


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

    def __call__(self, **kwargs: object) -> WorkerOutcome:
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
        return WorkerOutcome(0, transcript)


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

    def __call__(self, **kwargs: object) -> WorkerOutcome:
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
                                "mechanism": "Adapt one island-specific search decision.",
                                "pseudocode": ["measure progress", "apply adaptive decision"],
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
        elif "ROLE: strategy-integrator" in prompt:
            candidate = workspace / "candidate" / "solver.py"
            text = candidate.read_text(encoding="utf-8")
            if "target island 0" in prompt:
                text += "\n# improved-1\n"
                used = ["i1-effective"]
            else:
                text += "\n# improved-0\n"
                used = ["i0-effective"]
            candidate.write_text(text, encoding="utf-8")
            metadata = workspace / "candidate" / ".optiprofiler_evolve"
            metadata.mkdir()
            (metadata / "solver_explanation.json").write_text(
                json.dumps(
                    {
                        "schema": "solver_explanation/1",
                        "summary": "Preserve the island mechanism and add one verified donor strategy.",
                        "pseudocode": ["run local mechanism", "apply donor mechanism"],
                        "mechanisms": [
                            {
                                "name": "verified strategy transfer",
                                "description": "Integrates one controller-verified semantic strategy.",
                                "code_bindings": [{"file": "solver.py", "lines": [1, 8]}],
                            }
                        ],
                        "expected_strengths": ["mixed public set"],
                        "known_limitations": ["single scripted transfer"],
                        "evidence_consulted": ["transfer_strategies.json"],
                    }
                )
            )
            (workspace / "transfer_manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "strategy_transfer_manifest/1",
                        "attempted_strategy_ids": used,
                        "applied_strategy_ids": used,
                        "rejected_strategy_ids": [],
                        "summary": "Applied one compatible donor mechanism.",
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
        return WorkerOutcome(0, transcript)


class NonzeroAfterOutputResearchRunner(FullResearchPathRunner):
    """Model a candidate-producing CLI that exits after writing its solver."""

    def __call__(self, **kwargs: object) -> WorkerOutcome:
        result = super().__call__(**kwargs)
        if "ROLE: strategy-integrator" in str(kwargs["prompt"]):
            return WorkerOutcome(17, result.transcript)
        return result


class RegressingIntegratorRunner(FullResearchPathRunner):
    """An integrator whose evaluated variant is worse than the island's own solver."""

    def __call__(self, **kwargs: object) -> WorkerOutcome:
        result = super().__call__(**kwargs)
        if "ROLE: strategy-integrator" in str(kwargs["prompt"]):
            solver = kwargs["workspace"] / "candidate" / "solver.py"
            solver.write_text("def solver(fun, x0):\n    return x0\n# regressed\n")
        return result


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
    def test_research_roles_receive_only_projected_evidence_without_broker_calls(self):
        fixture = Path(__file__).parent / "fixtures" / "eval_report_v1"

        class PrivateEvidenceEvaluator(ResearchEvaluator):
            def evaluate(self, candidate, mode, output_dir):
                result = super().evaluate(candidate, mode, output_dir)
                benchmark = output_dir / "benchmark"
                shutil.copytree(fixture, benchmark)
                (output_dir / "private.json").write_text('{"secret": "PRIVATE_CANARY"}')
                if mode != "public_score":
                    (benchmark / "plot.svg").write_text("<svg>public plot</svg>")
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "solver.py"
            source.write_text("def solver(fun, x0):\n    return x0\n")
            raw = research_config()
            raw["execution"]["feedback_mode"] = "full_artifacts"
            seen = []
            scripted = ScriptedResearchRunner()

            def runner(**kwargs):
                if any(role in kwargs["prompt"] for role in
                       ("ROLE: direction-scout", "ROLE: strategy-analyst")):
                    evidence = kwargs["workspace"] / "public_evidence"
                    seen.append(evidence)
                    self.assertTrue((evidence / "feedback.md").is_file())
                    self.assertTrue((evidence / "artifact_index.json").is_file())
                    self.assertTrue((evidence / "benchmark/plot.svg").is_file())
                    self.assertFalse((evidence / "private.json").exists())
                    self.assertFalse(list(evidence.rglob("eval_report*")))
                return scripted(**kwargs)

            engine = EvolutionRun(
                initial=source, interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python", editable=("solver.py",), config=load_config(raw),
                run_dir=root / "run", worker_adapter=RunnerAdapter(runner),
                evaluator_factory=lambda **kwargs: PrivateEvidenceEvaluator(),
            )
            engine.run()
            self.assertEqual(len(seen), 3)
            self.assertTrue(list((root / "run/controller/evaluations").rglob("eval_report.json")))

    def test_strategy_analyst_keeps_model_transport_but_disables_web_search(self) -> None:
        phase = StrategyAnalysisPhase()

        self.assertTrue(phase.tools.network)
        self.assertFalse(phase.tools.web_search)

    def test_strategy_analysis_uses_smoke_ablation_by_default(self) -> None:
        self.assertEqual(StrategyAnalysisPhase().evaluation_mode, "smoke")

    def test_valid_role_outputs_survive_a_nonzero_cli_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
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
            raw = research_config()
            raw["workflow"]["phases"] = [
                {"name": "strategy_transfer"} if item["name"] == "recombine" else item
                for item in raw["workflow"]["phases"]
            ]
            engine = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(raw),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(NonzeroAfterOutputResearchRunner()),
                evaluator_factory=research_evaluator_factory,
            )

            result = engine.run()

            transfer = json.loads(
                (root / "run" / "research" / "strategy_transfer.json").read_text()
            )
            self.assertEqual(transfer["status"], "ok")
            self.assertTrue(result.best_candidate_id.startswith("transfer-i"))

    def test_strategy_transfer_keeps_one_base_per_island_and_borrows_only_supported_cards(self) -> None:
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
            raw = research_config()
            raw["workflow"]["phases"] = [
                {"name": "strategy_transfer"} if item["name"] == "recombine" else item
                for item in raw["workflow"]["phases"]
            ]
            prompts = {}
            scripted = FullResearchPathRunner()

            def capture(**kwargs):
                prompts[kwargs["workspace"].name] = kwargs["prompt"]
                return scripted(**kwargs)

            engine = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(raw),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(capture),
                evaluator_factory=research_evaluator_factory,
            )

            result = engine.run()
            for line in (root / "run/events.jsonl").read_text().splitlines():
                event = json.loads(line)
                if event["kind"] == "role_agent_started":
                    prompt = prompts[event["scope"]["job_id"]]
                    self.assertEqual(event["data"]["prompt_hash"],
                                     hashlib.sha256(prompt.encode()).hexdigest())

            transfer = json.loads(
                (root / "run" / "research" / "strategy_transfer.json").read_text()
            )
            self.assertEqual(transfer["status"], "ok")
            self.assertEqual(
                {item["island"] for item in transfer["islands"]},
                {0, 1},
            )
            for item in transfer["islands"]:
                self.assertEqual(len(item["donor_strategy_ids"]), 1)
                self.assertNotIn(
                    f"i{item['island']}-effective",
                    item["donor_strategy_ids"],
                )
                self.assertTrue(item["winner_candidate_id"].startswith("transfer-i"))
                role_dir = root / "run/research/roles/strategy-integrator" / item["winner_candidate_id"]
                # Inspect the actual unified-client prompt, not only the job's task template.
                trace = root / "run/research/traces/strategy-integrator" / item["winner_candidate_id"]
                self.assertTrue(role_dir.is_dir())
                prompt_text = "\n".join(path.read_text() for path in trace.rglob("*.txt"))
                self.assertIn("Performance:", prompt_text)
                self.assertIn("Semantic integrity:", prompt_text)
                self.assertIn("alongside candidate/, not inside candidate/", prompt_text)
                self.assertIn("parse it as JSON", prompt_text)
                self.assertNotIn("render_pdf", prompt_text)
                self.assertNotIn("artifact_index.json", prompt_text)
                self.assertNotIn("returned by the enabled public checks", prompt_text)
                winner_solver = Path(item["winner_solver"])
                self.assertTrue((winner_solver / "solver.py").is_file())
                explanation = root / "run" / "explanations" / (
                    item["winner_candidate_id"] + ".json"
                )
                self.assertTrue(explanation.is_file())
                self.assertEqual(json.loads(explanation.read_text())["status"], "complete")
            self.assertTrue((root / "run" / "FINAL_SOLVER_EXPLANATION.md").is_file())
            self.assertTrue(result.best_candidate_id.startswith("transfer-i"))
            final_explanation = (
                root / "run" / "FINAL_SOLVER_EXPLANATION.md"
            ).read_text(encoding="utf-8")
            winner_island = int(result.best_candidate_id.split("-i", 1)[1][:2])
            donor_island = 1 - winner_island
            self.assertIn(f"i{winner_island}-effective", final_explanation)
            self.assertIn(f"i{donor_island}-effective", final_explanation)
            self.assertNotIn("placebo", final_explanation)
            self.assertIn("transferred from island", final_explanation)
            self.assertIn("Adapt one island-specific search decision", final_explanation)
            self.assertIn("measure progress -> apply adaptive decision", final_explanation)

    def test_transfer_record_separates_an_evaluated_variant_from_the_retained_tree(self) -> None:
        """The real pilot: both variants were scored, yet both islands kept their base."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text(
                "def solver(fun, x0):\n    return x0\n# strategy-0-slot\n# strategy-1-slot\n"
                "# inert-0-slot\n# inert-1-slot\n")
            raw = research_config()
            raw["workflow"]["phases"] = [
                {"name": "strategy_transfer"} if item["name"] == "recombine" else item
                for item in raw["workflow"]["phases"]
            ]
            EvolutionRun(
                initial=source, interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python", editable=("solver.py",), config=load_config(raw),
                run_dir=root / "run", worker_adapter=RunnerAdapter(RegressingIntegratorRunner()),
                evaluator_factory=research_evaluator_factory,
            ).run()
            transfer = json.loads((root / "run/research/strategy_transfer.json").read_text())
            self.assertEqual(len(transfer["islands"]), 2)
            for item in transfer["islands"]:
                (attempt,) = item["attempts"]
                base = item["base"]
                # "succeeded" still only means built and scored (older readers keep working).
                self.assertEqual(attempt["status"], "succeeded")
                self.assertEqual((attempt["retained"], base["retained"]), (False, True))
                self.assertEqual(item["winner_candidate_id"], base["candidate_id"])
                self.assertEqual(item["selection"]["problem_set"], "validation")
                self.assertEqual(item["selection"]["candidates"],
                                 [base["candidate_id"], attempt["variant_id"]])
                self.assertEqual(attempt["validation"]["assessed_tree_hash"], attempt["tree_hash"])
                self.assertEqual(base["validation"]["assessed_tree_hash"], base["tree_hash"])
                self.assertLess(attempt["validation"]["performance_score"],
                                base["validation"]["performance_score"])
                for side in (attempt, base):
                    self.assertIn("status", side["validation"]["review_decision"])
                self.assertEqual(tree_hash(Path(item["winner_solver"])), base["tree_hash"])

    def test_strategy_transfer_still_materializes_each_island_winner_without_a_donor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
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
            raw = research_config()
            raw["evolution"]["islands"] = 1
            raw["workflow"]["phases"] = [
                {"name": "strategy_transfer"} if item["name"] == "recombine" else item
                for item in raw["workflow"]["phases"]
            ]

            EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(raw),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(FullResearchPathRunner()),
                evaluator_factory=research_evaluator_factory,
            ).run()

            transfer = json.loads(
                (root / "run" / "research" / "strategy_transfer.json").read_text()
            )
            self.assertEqual(transfer["status"], "skipped")
            self.assertEqual(len(transfer["islands"]), 1)
            self.assertTrue(transfer["islands"][0]["base"]["retained"])
            winner = Path(transfer["islands"][0]["winner_solver"])
            self.assertTrue((winner / "solver.py").is_file())

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
            engine = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(research_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(runner),
                evaluator_factory=research_evaluator_factory,
            )
            result = engine.run()

            self.assertEqual(tree_hash(source), source_hash)
            role_outcomes = sorted(
                (root / "run" / "research" / "traces").glob("*/*/outcome.json")
            )
            self.assertTrue(role_outcomes)
            self.assertTrue(
                all(json.loads(path.read_text())["state"] == "completed" for path in role_outcomes)
            )
            trace_index = [
                json.loads(line)
                for line in (root / "run" / "controller" / "trace_index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            role_entries = [entry for entry in trace_index if "role" in entry["join"]]
            self.assertEqual(
                {entry["join"]["role"] for entry in role_entries},
                {"direction-scout", "strategy-analyst"},
            )
            self.assertTrue(all(entry["join"]["module"] for entry in role_entries))
            strategy_entries = [
                entry
                for entry in role_entries
                if entry["join"]["role"] == "strategy-analyst"
            ]
            self.assertEqual(
                {entry["join"]["candidate_id"] for entry in strategy_entries},
                {"it001-i00-a00", "it001-i01-a00"},
            )
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
            for item in bundles:
                analysis = item["analysis"]
                self.assertEqual(analysis["status"], "verified")
                self.assertIsNone(analysis["reason"])
                matrix = json.loads(Path(analysis["trace"]["ablation_matrix"]).read_text())
                self.assertEqual(matrix["status"], "populated")
                self.assertTrue(matrix["entries"])
                self.assertTrue(matrix["entries"][0]["base_artifact_dirs"])
                self.assertTrue(matrix["entries"][0]["ablated_artifact_dirs"])
                # Scope and pairing are recorded; no gate or score depends on them.
                self.assertEqual(matrix["screen_scope"]["problem_set"], matrix["evaluation_mode"])
                self.assertEqual(matrix["screen_scope"]["n_repeats"], matrix["n_repeats"])
                self.assertIn("not evidence of a general effect",
                              matrix["screen_scope"]["interpretation"])
                for entry in matrix["entries"]:
                    paired = entry["paired_conditions"]
                    self.assertTrue(paired["identical"], paired)
                    self.assertEqual({item["mode"] for item in (*paired["base"], *paired["ablated"])},
                                     {"smoke"})
                    self.assertIn("ablated_public_scores", entry)  # legacy field kept
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
            engine = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(research_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(FullResearchPathRunner()),
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
            self.assertIn("combo-001", engine.state.research_finalists)
            self.assertTrue(
                {"bundle-island-0", "bundle-island-1"}.issubset(engine.state.research_finalists)
            )
            selection = json.loads((root / "run" / "validation_selection.json").read_text())
            self.assertIn("combo-001", selection["finalists"])
            self.assertEqual(result.best_candidate_id, "combo-001")
            self.assertAlmostEqual(result.public_score, 0.7)
            self.assertEqual(sum(mode == "hidden" for _name, mode in ResearchEvaluator.calls), 1)
            usage = json.loads((root / "run" / "research" / "validation_usage.json").read_text())
            self.assertGreaterEqual(usage["selection_calls"], 6)
            self.assertEqual(usage["new_evaluations"], 3)
            for candidate_id in ("bundle-island-0", "bundle-island-1", "combo-001"):
                self.assertEqual(engine.state.candidates[candidate_id].assessment["results"]["integrity"]["metrics"]["verdict"], "approve")
                self.assertTrue(
                    Path(engine.state.candidates[candidate_id].assessment["results"]["integrity"]["evidence"][0]).is_file()
                )

    def test_variant_gate_rejects_out_of_scope_and_conflicting_patches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            (source / "locked.txt").write_text("original\n")
            engine = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(ScriptedResearchRunner()),
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
        def failing_roles(**kwargs: object) -> WorkerOutcome:
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
            return WorkerOutcome(code, transcript)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            result = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(research_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(failing_roles),
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
            for item in bundles:
                analysis = item["analysis"]
                self.assertEqual(analysis["status"], "analyst_failed")
                self.assertIn("reason", analysis)
                self.assertTrue(analysis["reason"])
                self.assertFalse(analysis["declared_by_analyst"])
                trace = analysis["trace"]
                self.assertTrue(trace["analyst_job_id"].startswith("analysis-island-"))
                self.assertTrue(Path(trace["strategy_cards"]).is_file())
                self.assertTrue(Path(trace["ablation_matrix"]).is_file())
                matrix = json.loads(Path(trace["ablation_matrix"]).read_text())
                self.assertEqual(matrix["entries"], [])
                self.assertEqual(matrix["status"], "analyst_failed")
                self.assertTrue(matrix["reason"])


    def test_declared_not_decomposable_is_structured_never_silent(self) -> None:
        def declaring_roles(**kwargs: object) -> WorkerOutcome:
            workspace = kwargs["workspace"]
            prompt = str(kwargs["prompt"])
            transcript = kwargs["transcript"]
            assert isinstance(workspace, Path)
            assert isinstance(transcript, Path)
            if "ROLE:" not in prompt:
                solver = workspace / "solver.py"
                solver.write_text(solver.read_text() + "\n# improved\n")
            elif "ROLE: strategy-analyst" in prompt:
                (workspace / "strategy_cards.json").write_text(
                    json.dumps(
                        {
                            "cards": [],
                            "not_decomposable": {
                                "reason": "single inseparable step-size rewrite"
                            },
                        }
                    )
                )
            elif "ROLE: direction-scout" in prompt:
                (workspace / "direction_cards.json").write_text(
                    json.dumps({"cards": []})
                )
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("scripted", encoding="utf-8")
            return WorkerOutcome(0, transcript)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            result = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(research_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(declaring_roles),
                evaluator_factory=research_evaluator_factory,
            ).run()
            self.assertEqual(result.public_score, 0.7)

            bundles = json.loads(
                (root / "run" / "research" / "analysis" / "island_bundles.json").read_text()
            )["bundles"]
            self.assertTrue(bundles)
            for item in bundles:
                analysis = item["analysis"]
                self.assertEqual(analysis["status"], "not_decomposable")
                self.assertIn("single inseparable step-size rewrite", analysis["reason"])
                self.assertTrue(analysis["declared_by_analyst"])
                self.assertTrue(Path(analysis["trace"]["analyst_transcript"]).is_file())
                matrix = json.loads(
                    Path(analysis["trace"]["ablation_matrix"]).read_text()
                )
                self.assertEqual(matrix["entries"], [])
                self.assertEqual(matrix["status"], "not_decomposable")
                self.assertIn("inseparable", matrix["reason"])
            recombination = json.loads(
                (root / "run" / "research" / "recombination.json").read_text()
            )
            self.assertEqual(recombination["status"], "skipped")
            self.assertTrue(recombination["reason"])


    def test_capped_ablation_reason_states_evaluated_supported_untested(self) -> None:
        def capped_runner(**kwargs: object) -> WorkerOutcome:
            workspace = kwargs["workspace"]
            prompt = str(kwargs["prompt"])
            transcript = kwargs["transcript"]
            assert isinstance(workspace, Path)
            assert isinstance(transcript, Path)
            if "ROLE: direction-scout" in prompt:
                (workspace / "direction_cards.json").write_text(json.dumps({"cards": []}))
            elif "ROLE: strategy-analyst" in prompt:
                cards = []
                for index in (1, 2, 3):
                    variant = workspace / "variants" / f"p{index}"
                    shutil.copytree(workspace / "finalist", variant)
                    solver = variant / "solver.py"
                    solver.write_text(
                        solver.read_text(encoding="utf-8").replace(
                            f"# inert{index}\n", ""
                        ),
                        encoding="utf-8",
                    )
                    cards.append(
                        {
                            "strategy_id": f"s{index}",
                            "claim": f"inert marker {index} is useful",
                            "code_bindings": [{"file": "solver.py", "lines": [1, 5]}],
                            "toggle": {
                                "kind": "variant_tree",
                                "ref": f"variants/p{index}",
                            },
                        }
                    )
                (workspace / "strategy_cards.json").write_text(
                    json.dumps({"cards": cards}), encoding="utf-8"
                )
            else:
                solver = workspace / "solver.py"
                solver.write_text(
                    solver.read_text(encoding="utf-8")
                    + "\n# improved\n# inert1\n# inert2\n# inert3\n",
                    encoding="utf-8",
                )
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("scripted", encoding="utf-8")
            return WorkerOutcome(0, transcript)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            raw = research_config()
            for phase in raw["workflow"]["phases"]:
                if phase["name"] == "strategy_analysis":
                    phase["options"].update({"max_strategies": 4, "max_ablations": 1})
            EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(raw),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(capped_runner),
                evaluator_factory=research_evaluator_factory,
            ).run()

            bundles = json.loads(
                (root / "run" / "research" / "analysis" / "island_bundles.json").read_text()
            )["bundles"]
            expected = (
                "3 strategies proposed; 1 evaluated by leave-one-out ablation, "
                "0 supported, 2 untested (max_ablations=1)"
            )
            self.assertTrue(bundles)
            for item in bundles:
                analysis = item["analysis"]
                self.assertEqual(analysis["status"], "unverified")
                self.assertEqual(analysis["reason"], expected)
                matrix = json.loads(
                    Path(analysis["trace"]["ablation_matrix"]).read_text()
                )
                self.assertEqual(matrix["status"], "populated")
                self.assertEqual(matrix["reason"], expected)
                self.assertEqual(len(matrix["entries"]), 1)
                self.assertEqual(matrix["entries"][0]["conclusion"], "placebo")

    def test_recombination_records_conflict_instead_of_force_merging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            engine = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(ScriptedResearchRunner()),
                evaluator_factory=research_evaluator_factory,
            )
            engine._initialize_run_directory()
            prepared = engine.coordinator.run_phase(PreparePhase(), {})["prepared"]
            seed = prepared.seed
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
            engine = EvolutionRun(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=("solver.py",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                worker_adapter=RunnerAdapter(ScriptedResearchRunner()),
                evaluator_factory=research_evaluator_factory,
            )
            engine._initialize_run_directory()
            prepared = engine.coordinator.run_phase(PreparePhase(), {})["prepared"]
            seed = prepared.seed
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
                    emit=lambda _kind, _status, _data=None: None,
                    services=engine.controller_services,
                )
            )

            payload = json.loads(Path(str(result.artifacts["combinations"])).read_text())
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["base_candidate"], "seed")


if __name__ == "__main__":
    unittest.main()
