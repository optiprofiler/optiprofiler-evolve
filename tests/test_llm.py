from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from test_config import minimal_config
from worker_helpers import RunnerAdapter

from optiprofiler_evolve.agents.llm import LLMClient
from optiprofiler_evolve.observability.trace_ledger import TraceLedger
from optiprofiler_evolve.shared.config import load_config
from optiprofiler_evolve.shared.protocols import AgentJob, WorkerOutcome, WorkerRequest


class LLMClientTests(unittest.TestCase):
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
        self.assertEqual(result.outputs, {"answer.json": self.workspace / "answer.json"})
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
