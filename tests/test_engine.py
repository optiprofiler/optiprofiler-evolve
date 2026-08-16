from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path

from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.engine import EvolutionEngine, _controller_cancellation
from optiprofiler_evolve.models import EvaluationResult
from optiprofiler_evolve.review import REQUIRED_CHECKS
from optiprofiler_evolve.sandbox import AgentRunResult
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash

from test_config import minimal_config


class FakeEvaluator:
    calls: list[str] = []

    def evaluate(self, candidate: Path, mode: str, output_dir: Path) -> EvaluationResult:
        self.calls.append(mode)
        output_dir.mkdir(parents=True, exist_ok=True)
        improved = "improved" in (candidate / "solver.py").read_text()
        score = 0.7 if improved else 0.5
        if mode == "validation" and improved:
            score = 0.68
        if mode == "hidden" and improved:
            score = 0.65
        result = EvaluationResult(mode, score, score, 0.5, 1, output_dir)
        (output_dir / "result.json").write_text(json.dumps(result.as_dict(), default=str))
        (output_dir / "feedback.md").write_text("ok")
        return result


def fake_evaluator_factory(**_kwargs) -> FakeEvaluator:
    return FakeEvaluator()


def fake_agent_runner(**kwargs) -> AgentRunResult:
    workspace = kwargs["workspace"]
    transcript = kwargs["transcript"]
    solver = workspace / "solver.py"
    solver.write_text(solver.read_text() + "\n# improved\n")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("fake worker\n")
    return AgentRunResult(0, transcript)


class ReviewingRunner:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.review_calls = 0

    def __call__(self, **kwargs) -> AgentRunResult:
        workspace = kwargs["workspace"]
        transcript = kwargs["transcript"]
        prompt = kwargs["prompt"]
        transcript.parent.mkdir(parents=True, exist_ok=True)
        if "independent integrity reviewer" not in prompt:
            solver = workspace / "solver.py"
            solver.write_text(solver.read_text() + "\n# improved\n")
            transcript.write_text("mutation worker\n")
            return AgentRunResult(0, transcript)

        self.review_calls += 1
        transcript.write_text(f"review attempt {self.review_calls}\n")
        if self.mode == "unavailable":
            return AgentRunResult(1, transcript)
        if self.mode == "malformed_once" and self.review_calls == 1:
            (workspace / "review.json").write_text('{"schema": "wrong"}')
            return AgentRunResult(0, transcript)
        findings = []
        verdict = "approve"
        if self.mode == "quarantine":
            verdict = "quarantine"
            findings = [
                {
                    "category": "problem_hardcoding",
                    "severity": "high",
                    "summary": "case-specific code",
                    "evidence": [
                        {
                            "path": "candidate/solver.py",
                            "line": 1,
                            "detail": "candidate contains a fixed case branch",
                        }
                    ],
                }
            ]
        (workspace / "review.json").write_text(
            json.dumps(
                {
                    "schema": "integrity_review/1",
                    "verdict": verdict,
                    "checked": sorted(REQUIRED_CHECKS),
                    "summary": f"review {verdict}",
                    "findings": findings,
                }
            )
        )
        return AgentRunResult(0, transcript)


def reviewer_config(*, strict: bool = False) -> dict:
    raw = minimal_config()
    raw["evolution"].update({"islands": 1, "attempts_per_island": 1})
    raw["integrity_review"] = {
        "component": {"name": "agent_integrity"},
        "allow_same_model": True,
        "retries": 1,
        "strict": strict,
    }
    return raw


class EngineTests(unittest.TestCase):

    def test_solver_contract_exposes_candidate_dependency_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            raw = minimal_config()
            raw["evaluation"]["forbidden_candidate_imports"] = ["scipy.optimize", "prima"]
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(raw),
                run_dir=root / "run",
                agent_runner=fake_agent_runner,
                evaluator_factory=fake_evaluator_factory,
            )

            engine._phase_prepare()

            contract = json.loads((root / "run" / "solver_contract.json").read_text())
            self.assertEqual(
                contract["forbidden_candidate_imports"],
                ["scipy.optimize", "prima"],
            )

    def test_live_run_updates_phase_page_between_attempts(self) -> None:
        captured: dict[str, str] = {}
        state = {"calls": 0}

        def observing_runner(**kwargs) -> AgentRunResult:
            workspace = kwargs["workspace"]
            transcript = kwargs["transcript"]
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("worker\n")
            solver = workspace / "solver.py"
            solver.write_text(solver.read_text() + "\n# improved\n")
            state["calls"] += 1
            if state["calls"] == 2:
                time.sleep(0.5)
                page = kwargs["run_page"]
                captured["text"] = (
                    page.read_text(encoding="utf-8") if page.is_file() else ""
                )
            return AgentRunResult(0, transcript)

        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            raw = minimal_config()
            raw["evolution"].update({"islands": 1, "attempts_per_island": 2})
            raw["workers"]["max_parallel"] = 1
            page = root / "run" / "owner" / "phases" / "explore.html"

            def runner(**kwargs) -> AgentRunResult:
                kwargs["run_page"] = page
                return observing_runner(**kwargs)

            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(raw),
                run_dir=root / "run",
                agent_runner=runner,
                evaluator_factory=fake_evaluator_factory,
            )
            engine._refresh_poll_seconds = 0.05
            engine._refresh_due_seconds = 0.05
            engine.run()

            text = captured["text"]
            # While attempt 2's worker was still running, the on-disk explore
            # page already carried attempt 1 from later ledger events, and the
            # live page instructs the browser to keep reloading.
            self.assertIn("it001-i00-a00", text)
            self.assertIn('http-equiv="refresh"', text)


    def test_background_refresher_rerenders_between_engine_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(minimal_config()),
                run_dir=root / "run",
                agent_runner=fake_agent_runner,
                evaluator_factory=fake_evaluator_factory,
            )
            calls: list[int] = []
            engine._refresh_status = lambda **_kwargs: calls.append(1)
            engine._refresh_poll_seconds = 0.02
            engine._last_status_refresh = 0.0
            thread = threading.Thread(target=engine._refresh_status_loop, daemon=True)
            thread.start()
            time.sleep(0.3)
            engine._refresh_stop.set()
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(len(calls), 1)


    def test_nonzero_worker_exit_blocks_admission_and_keeps_evidence(self) -> None:
        def budget_exhausted_runner(**kwargs) -> AgentRunResult:
            workspace = kwargs["workspace"]
            transcript = kwargs["transcript"]
            solver = workspace / "solver.py"
            solver.write_text(solver.read_text() + "\n# improved but unfinished\n")
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("ran out of budget mid-edit\n")
            return AgentRunResult(
                1, transcript, termination_reason="error_max_budget_usd"
            )

        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            config = load_config(minimal_config())
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=config,
                run_dir=root / "run",
                agent_runner=budget_exhausted_runner,
                evaluator_factory=fake_evaluator_factory,
            )
            result = engine.run()

            # The half-finished workspace never becomes a candidate: the seed
            # stays champion and no attempt reaches smoke/public evaluation.
            self.assertEqual(result.public_score, 0.5)
            self.assertNotIn("improved", (result.best_solver / "solver.py").read_text())
            self.assertEqual(FakeEvaluator.calls.count("public_score"), 1)
            self.assertEqual(FakeEvaluator.calls.count("smoke"), 0)

            events = [
                json.loads(line)
                for line in (result.run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            finished = [
                event for event in events if event["kind"] == "attempt_finished"
            ]
            self.assertTrue(finished)
            for event in finished:
                self.assertEqual(event["status"], "failed")
                self.assertFalse(event["data"]["accepted"])
                self.assertFalse(event["data"]["valid"])
                self.assertIn("worker_failed_not_admitted", event["data"]["error"])
                self.assertIn("error_max_budget_usd", event["data"]["error"])
                self.assertIsNone(event["data"]["public_score"])
            mutate_steps = [
                event
                for event in events
                if event["kind"] == "step_finished"
                and event["scope"].get("step") == "mutate"
            ]
            self.assertTrue(mutate_steps)
            for event in mutate_steps:
                self.assertEqual(event["status"], "failed")
            # Rejection precedes audit, evaluation, and integrity review.
            self.assertFalse(
                [
                    event
                    for event in events
                    if event["kind"].startswith("integrity_review")
                ]
            )
            later_steps = [
                event
                for event in events
                if event["kind"] == "step_finished"
                and event["scope"].get("step") in {"static_audit", "smoke", "public_evaluate"}
            ]
            self.assertEqual(later_steps, [])

            # Evidence stays on disk for post-mortem analysis.
            workspace = result.run_dir / "workspaces" / "it001-i00-a00"
            self.assertIn(
                "improved but unfinished", (workspace / "solver.py").read_text()
            )
            transcript = result.run_dir / "transcripts" / "it001-i00-a00.jsonl"
            self.assertIn("ran out of budget", transcript.read_text())

    def test_population_loop_preserves_source_and_tests_one_validated_champion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            original_hash = tree_hash(source)
            config = load_config(minimal_config())
            engine = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=config,
                run_dir=root / "run",
                agent_runner=fake_agent_runner,
                evaluator_factory=fake_evaluator_factory,
            )
            result = engine.run()
            self.assertEqual(tree_hash(source), original_hash)
            self.assertEqual(result.public_score, 0.7)
            self.assertEqual(result.validation_score, 0.68)
            self.assertEqual(result.final_score, 0.65)
            self.assertEqual(FakeEvaluator.calls.count("public_score"), 3)
            self.assertEqual(FakeEvaluator.calls.count("hidden"), 1)
            self.assertIn("improved", (result.best_solver / "solver.py").read_text())
            self.assertEqual(result.best_solver, result.run_dir / "final_solver")
            self.assertTrue((result.run_dir / "final_solver" / "solver.py").is_file())
            self.assertTrue(
                (result.run_dir / "candidates" / "it001-i00-a00" / "solver.py").is_file()
            )
            first_workspace = result.run_dir / "workspaces" / "it001-i00-a00"
            second_workspace = result.run_dir / "workspaces" / "it001-i01-a00"
            self.assertTrue(first_workspace.is_dir())
            self.assertTrue(second_workspace.is_dir())
            self.assertNotEqual(first_workspace.resolve(), second_workspace.resolve())
            self.assertNotEqual(first_workspace.resolve(), source.resolve())
            public_manifest = (result.run_dir / "public_data_manifest.json").read_text()
            self.assertNotIn("controller/data_manifest", public_manifest)
            self.assertNotIn("P1", public_manifest)
            self.assertTrue((result.run_dir / "checkpoints" / "iteration_001.json").is_file())
            for trace_root in sorted((result.run_dir / "traces").iterdir()):
                self.assertEqual((trace_root / "raw.stdout.stream").read_text(), "fake worker\n")
                terminal = json.loads((trace_root / "outcome.json").read_text())
                self.assertEqual(terminal["state"], "completed")
                self.assertIn("without native chunk timing", terminal["capture_error"])

    def test_agent_reviewer_approves_before_candidate_validation_and_keeps_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            runner = ReviewingRunner("approve")
            result = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(reviewer_config()),
                run_dir=root / "run",
                agent_runner=runner,
                evaluator_factory=fake_evaluator_factory,
            ).run()
            self.assertEqual(runner.review_calls, 1)
            self.assertEqual(FakeEvaluator.calls.count("validation"), 2)
            review_root = (
                result.run_dir
                / "research"
                / "traces"
                / "integrity-reviewer"
                / "it001-i00-a00-r01"
            )
            self.assertTrue((review_root / "raw.stdout.stream").is_file())
            self.assertEqual(json.loads((review_root / "outcome.json").read_text())["state"], "completed")
            decision = json.loads(
                (
                    result.run_dir
                    / "controller"
                    / "integrity_reviews"
                    / "it001-i00-a00"
                    / "decision.json"
                ).read_text()
            )
            self.assertEqual(decision["verdict"], "approve")
            public_events = (result.run_dir / "public_events.jsonl").read_text()
            self.assertIn('"gate": "approved"', public_events)
            self.assertNotIn("finding_count", public_events)
            self.assertNotIn("integrity_reviews/", public_events)
            reviewer_workspace = (
                result.run_dir
                / "research"
                / "roles"
                / "integrity-reviewer"
                / "it001-i00-a00-r01"
            )
            self.assertIn(
                "mutation worker",
                (reviewer_workspace / "mutation_transcript.txt").read_text(),
            )
            trace_entries = [
                json.loads(line)
                for line in (result.run_dir / "controller" / "trace_index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            reviewer_entry = next(
                entry
                for entry in trace_entries
                if entry["join"].get("role") == "integrity-reviewer"
            )
            self.assertEqual(reviewer_entry["join"]["candidate_id"], "it001-i00-a00")
            self.assertEqual(reviewer_entry["join"]["module"], "integrity-reviewer")

    def test_quarantined_candidate_never_consumes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            runner = ReviewingRunner("quarantine")
            result = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(reviewer_config()),
                run_dir=root / "run",
                agent_runner=runner,
                evaluator_factory=fake_evaluator_factory,
            ).run()
            self.assertEqual(FakeEvaluator.calls.count("validation"), 1)
            attempt = next(
                json.loads(line)
                for line in (result.run_dir / "attempts.jsonl").read_text().splitlines()
            )
            self.assertFalse(attempt["valid"])
            self.assertEqual(attempt["review_verdict"], "quarantine")

    def test_malformed_review_retries_then_approves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            runner = ReviewingRunner("malformed_once")
            EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(reviewer_config()),
                run_dir=root / "run",
                agent_runner=runner,
                evaluator_factory=fake_evaluator_factory,
            ).run()
            self.assertEqual(runner.review_calls, 2)
            self.assertEqual(FakeEvaluator.calls.count("validation"), 2)

    def test_strict_reviewer_outage_aborts_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            with self.assertRaisesRegex(RuntimeError, "unavailable in strict mode"):
                EvolutionEngine(
                    initial=source,
                    interface=InterfaceSpec.parse("solver.py:solver"),
                    runtime="python",
                    editable=(".",),
                    config=load_config(reviewer_config(strict=True)),
                    run_dir=root / "run",
                    agent_runner=ReviewingRunner("unavailable"),
                    evaluator_factory=fake_evaluator_factory,
                ).run()

    def test_nonstrict_reviewer_outage_quarantines_and_run_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FakeEvaluator.calls = []
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("def solver(fun, x0):\n    return x0\n")
            runner = ReviewingRunner("unavailable")
            result = EvolutionEngine(
                initial=source,
                interface=InterfaceSpec.parse("solver.py:solver"),
                runtime="python",
                editable=(".",),
                config=load_config(reviewer_config(strict=False)),
                run_dir=root / "run",
                agent_runner=runner,
                evaluator_factory=fake_evaluator_factory,
            ).run()
            self.assertEqual(runner.review_calls, 2)
            self.assertEqual(FakeEvaluator.calls.count("validation"), 1)
            decision = json.loads(
                (
                    result.run_dir
                    / "controller"
                    / "integrity_reviews"
                    / "it001-i00-a00"
                    / "decision.json"
                ).read_text()
            )
            self.assertEqual(decision["verdict"], "quarantine")
            self.assertEqual(decision["reason"], "reviewer_unavailable")

    @unittest.skipUnless(os.name == "posix", "signal test requires POSIX")
    def test_controller_signal_scope_requests_cooperative_cancellation(self) -> None:
        event = threading.Event()
        with _controller_cancellation(event):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(event.wait(timeout=1))


if __name__ == "__main__":
    unittest.main()
