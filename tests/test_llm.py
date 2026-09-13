from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from test_config import minimal_config
from test_prompt import _prompt_kwargs
from worker_helpers import RunnerAdapter

from optiprofiler_evolve.agents.contracts import AgentJob, SolverJob, WorkerOutcome, WorkerRequest
from optiprofiler_evolve.agents.llm import LLMClient
from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.observability.trace_ledger import TraceLedger
from optiprofiler_evolve.phases.attempts.prompt import build_worker_prompt
from optiprofiler_evolve.phases.strategy_transfer import _strategy_transfer_prompt


class LLMClientTests(unittest.TestCase):
    def test_both_solver_roles_receive_the_complete_explanation_contract(self):
        tasks = {
            'explorer': build_worker_prompt(**_prompt_kwargs()),
            'strategy-integrator': _strategy_transfer_prompt(
                island=0, base_candidate='seed', strategy_ids=['strategy'],
                prompt_version='strategy-transfer/2',
            ),
        }
        for role, prompt in tasks.items():
            with self.subTest(role=role):
                self.request = replace(self.request, trace_dir=self.root / role)
                client = self.client(lambda kwargs: WorkerOutcome(0, kwargs['transcript']))
                client.generation_guidance = Mock(return_value='CRITERION_GUIDANCE')
                client.run(SolverJob(role, role, prompt))
                delivered = self.calls[-1]['prompt']
                self.assertEqual(delivered.count('CRITERION_GUIDANCE'), 1)
                self.assertEqual(delivered.count('schema `solver_explanation/1`'), 1)
                self.assertIn('"schema": "solver_explanation/1"', delivered)
                for field in ('summary', 'pseudocode', 'mechanisms', 'name', 'description',
                              'code_bindings', 'file', 'lines', 'expected_strengths',
                              'known_limitations', 'evidence_consulted'):
                    self.assertIn(f'`{field}`', delivered)
                self.assertIn('one-based `[start, end]`', delivered)
                self.assertIn('1 MiB', delivered)
                self.assertNotIn('contract from the normal worker task', delivered)
                if role == 'strategy-integrator':
                    self.assertIn('`candidate/.optiprofiler_evolve/solver_explanation.json`', delivered)
                    self.assertIn('no\nevaluation broker', delivered)

    def test_solver_guidance_is_rendered_once_and_recorded_in_the_trace(self) -> None:
        client = self.client(lambda kwargs: WorkerOutcome(0, kwargs["transcript"]))
        guidance = Mock(return_value="SYNTHETIC_CRITERION_GUIDANCE")
        client.generation_guidance = guidance
        job = SolverJob("explorer", "job", "Edit the solver.")
        started = Mock()
        client.run(job, on_started=started)
        guidance.assert_called_once_with()
        self.assertEqual(job.prompt, "Edit the solver.")
        prompt = self.calls[0]["prompt"]
        self.assertEqual(prompt, f"{job.prompt}\n\n{guidance.return_value}")
        self.assertEqual(prompt.count(guidance.return_value), 1)
        started.assert_called_once()
        self.assertEqual(started.call_args.args[0].prompt, prompt)
        self.assertTrue(any(guidance.return_value in p.read_text() for p in
                            self.request.trace_dir.rglob("*.txt")))

    def test_review_job_never_requests_generation_guidance(self) -> None:
        client = self.client(lambda kwargs: WorkerOutcome(0, kwargs["transcript"]))
        client.generation_guidance = Mock(side_effect=AssertionError("review is not generation"))
        job = AgentJob("integrity-reviewer", "job", "Review the solver independently.")
        client.run(job)
        client.generation_guidance.assert_not_called()
        self.assertEqual(self.calls[0]["prompt"], job.prompt)

    def test_solver_job_cannot_silently_omit_criterion_guidance(self) -> None:
        client = self.client(lambda kwargs: WorkerOutcome(0, kwargs["transcript"]))
        with self.assertRaisesRegex(ValueError, "controller-bound criterion guidance"):
            client.run(SolverJob("explorer", "job", "Edit."))
        self.assertFalse(self.calls)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = load_config(minimal_config())
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.cancel = threading.Event()
        self.request = WorkerRequest(
            worker=self.config.workers.pool[0],
            workers=self.config.workers,
            sandbox=self.config.sandbox,
            workspace=self.workspace,
            tools_dir=self.root / "tools",
            broker=object(),
            prompt="bound placeholder",
            transcript=self.root / "transcript.txt",
            trace_dir=self.root / "trace",
            trace_context={"join": {"phase": "explore", "attempt_id": "test"}},
            cancellation_event=self.cancel,
        )
        self.calls = []
        self.usage = []

    def client(self, behavior) -> LLMClient:
        def run(**kwargs):
            self.calls.append(kwargs)
            kwargs["transcript"].write_text("synthetic agent output")
            return behavior(kwargs)

        adapter = RunnerAdapter(run)
        return LLMClient(
            adapter=adapter,
            request=self.request,
            ledger=TraceLedger(
                self.root, run_id="test", config_hash="test", adapter_name=adapter.name
            ),
            emit=lambda *args, **kwargs: None,
            record_usage=lambda request, outcome: self.usage.append((request, outcome)),
        )

    def test_job_uses_bound_authority_and_records_one_invocation(self) -> None:
        def behavior(kwargs):
            (kwargs["workspace"] / "answer.json").write_text("{}")
            return WorkerOutcome(0, kwargs["transcript"])

        job = AgentJob(
            role="analyst",
            job_id="job",
            prompt="Inspect the supplied solver.",
            expected_outputs=("answer.json",),
            worker=replace(self.request.worker, model="not-the-bound-worker"),
        )
        result = self.client(behavior).run(job)
        self.assertTrue(result.success)
        self.assertEqual(
            result.outputs, {"answer.json": (self.workspace / "answer.json").resolve()}
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.usage), 1)
        self.assertEqual(self.calls[0]["prompt"], job.prompt)
        self.assertEqual(self.calls[0]["worker"], self.request.worker)
        self.assertIs(self.calls[0]["broker"], self.request.broker)
        self.assertEqual(self.calls[0]["workspace"], self.workspace)
        self.assertTrue(result.outcome.trace_outcome.is_file())
        index = (self.root / "controller/trace_index.jsonl").read_text().splitlines()
        self.assertEqual(len(index), 1)
        self.assertEqual(json.loads(index[0])["join"]["attempt_id"], "test")

    def test_missing_output_is_not_a_successful_job(self) -> None:
        client = self.client(lambda kwargs: WorkerOutcome(0, kwargs["transcript"]))
        result = client.run(
            AgentJob("analyst", "job", "inspect", expected_outputs=("missing.json",))
        )
        self.assertFalse(result.success)
        self.assertEqual(result.missing_outputs, ("missing.json",))
        self.assertTrue(result.outcome.trace_outcome.is_file())

    def test_backend_exception_still_finalizes_trace_and_usage(self) -> None:
        def fail(_kwargs):
            raise RuntimeError("synthetic backend failure")

        result = self.client(fail).run(AgentJob("analyst", "job", "inspect"))
        self.assertFalse(result.success)
        self.assertEqual(result.outcome.termination_reason, "adapter_exception")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.usage), 1)
        self.assertIn("synthetic backend failure", result.outcome.transcript.read_text())
        self.assertTrue(result.outcome.trace_outcome.is_file())

    def test_nonzero_recovery_never_recovers_timeout_or_cancellation(self) -> None:
        for recover, timed_out, cancelled, expected in (
            (False, False, False, False),
            (True, False, False, True),
            (True, True, False, False),
            (True, False, True, False),
        ):
            with self.subTest(recover=recover, timed_out=timed_out, cancelled=cancelled):
                suffix = f"{recover}-{timed_out}-{cancelled}"
                self.request = replace(self.request, trace_dir=self.root / suffix)

                def behavior(kwargs):
                    return WorkerOutcome(
                        1, kwargs["transcript"], timed_out=timed_out, cancelled=cancelled
                    )

                result = self.client(behavior).run(
                    AgentJob(
                        "analyst",
                        suffix,
                        "inspect",
                        recover_outputs_on_nonzero_exit=recover,
                    )
                )
                self.assertEqual(result.success, expected)
                self.assertTrue(result.outcome.trace_outcome.is_file())

    def test_output_symlink_cannot_expose_a_host_file(self) -> None:
        private = self.root / "private.txt"
        private.write_text("private canary")

        def behavior(kwargs):
            (kwargs["workspace"] / "answer.txt").symlink_to(private)
            return WorkerOutcome(0, kwargs["transcript"])

        result = self.client(behavior).run(
            AgentJob(
                "analyst",
                "job",
                "inspect",
                expected_outputs=("answer.txt",),
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(result.outputs, {})
        self.assertEqual(result.missing_outputs, ("answer.txt",))
