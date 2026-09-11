import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from optiprofiler_evolve.controller.commits import RunCommits
from optiprofiler_evolve.controller.coordinator import RunCoordinator
from optiprofiler_evolve.controller.state import RunState
from optiprofiler_evolve.errors import RunCancelled
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = RunState(phase_results=[PhaseResult(artifacts={"input": "public", "private": "secret"})])
        self.commits = RunCommits(self.state, Mock(), Path(directory.name), Mock(), Mock(), Mock())
        self.coordinator = RunCoordinator(
            state=self.state,
            phases=(),
            run_dir=Path(directory.name),
            services=Mock(),
            commits=self.commits,
            contexts={},
            emit=Mock(),
            refresh=Mock(),
            raise_if_cancelled=Mock(),
        )

    def phase(self, run):
        return SimpleNamespace(
            name="custom", requires=frozenset({"input"}), provides=frozenset({"output"}), run=run
        )

    def test_phase_sees_only_declared_read_only_artifacts(self):
        def run(context):
            self.assertIsInstance(context, PhaseContext)
            self.assertEqual(dict(context.artifacts), {"input": "public"})
            with self.assertRaises(TypeError):
                context.artifacts["input"] = "changed"
            with self.assertRaises(TypeError):
                context.options["option"] = "changed"
            return PhaseResult(artifacts={"output": "done"})

        self.coordinator.run_phase(self.phase(run), {})
        self.assertEqual(self.state.artifacts["output"], "done")
        self.assertEqual(self.state.artifacts["private"], "secret")

    def test_custom_context_factory_needs_no_central_dispatch_change(self):
        seen = Mock(return_value=PhaseResult(artifacts={"output": "done"}))
        factory = Mock(side_effect=lambda base: ("custom context", base))
        self.coordinator.contexts["custom"] = factory
        self.coordinator.run_phase(self.phase(seen), {})
        self.assertEqual(seen.call_args.args[0][0], "custom context")
        factory.assert_called_once()

    def test_optional_inputs_are_explicit_and_need_not_exist(self):
        observed = []
        phase = self.phase(
            lambda context: observed.append(dict(context.artifacts))
            or PhaseResult(artifacts={"output": "done"})
        )
        phase.optional_requires = frozenset({"directions"})
        self.coordinator.run_phase(phase, {})
        self.commits.commit_phase_result(PhaseResult(artifacts={"directions": "scout evidence"}))
        self.coordinator.run_phase(phase, {})
        self.assertEqual(
            observed, [{"input": "public"}, {"input": "public", "directions": "scout evidence"}]
        )

    def test_failed_result_is_rejected_even_at_commit_entry(self):
        with self.assertRaises(ValueError):
            self.commits.commit_phase_result(PhaseResult(status="failed", artifacts={"bad": 1}))
        self.assertNotIn("bad", self.state.artifacts)

    def test_failed_and_undeclared_results_never_commit_artifacts(self):
        for result in (
            PhaseResult(status="failed", artifacts={"output": "bad"}),
            PhaseResult(artifacts={"undeclared": "bad"}),
        ):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                self.coordinator.run_phase(self.phase(lambda _: result), {})
            self.assertEqual(self.state.artifacts, {"input": "public", "private": "secret"})

    def test_skipped_phase_can_publish_its_declared_evidence(self):
        self.coordinator.run_phase(
            self.phase(lambda _: PhaseResult(status="skipped", artifacts={"output": "skipped"})),
            {},
        )
        self.assertEqual(self.state.artifacts["output"], "skipped")

    def test_missing_declared_outputs_do_not_commit_even_when_skipped(self):
        for status in ("succeeded", "skipped"):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "omitted"):
                self.coordinator.run_phase(self.phase(lambda _: PhaseResult(status=status)), {})
            self.assertNotIn("output", self.state.artifacts)

    def test_cancelled_result_preserves_cancellation_and_does_not_publish(self):
        with self.assertRaises(RunCancelled):
            self.coordinator.run_phase(
                self.phase(lambda _: PhaseResult(status="cancelled", artifacts={"output": "bad"})),
                {},
            )
        self.assertNotIn("output", self.state.artifacts)

    def test_run_state_instances_do_not_share_mutable_values(self):
        other = RunState()
        self.assertEqual(other.artifacts, {})
        with self.assertRaises(AttributeError):
            other.implicit_research_state = {}

    def test_lower_layers_do_not_import_application(self):
        package = Path(inspect.getfile(RunCoordinator)).parents[1]
        for folder in ("controller", "phases", "agents", "criteria", "observability"):
            for path in (package / folder).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse(
                            (node.module or "").startswith("optiprofiler_evolve.application"),
                            str(path),
                        )

    def test_coordinator_does_not_import_execution_implementations(self):
        source = inspect.getsource(inspect.getmodule(RunCoordinator))
        for forbidden in (
            "docker",
            "LLMClient",
            "broker",
            "radon",
            "IntegrityReview",
            "render_status",
        ):
            self.assertNotIn(forbidden, source)

    def test_production_state_writes_belong_to_commits(self):
        package = Path(inspect.getfile(RunCoordinator)).parents[1]
        for path in package.rglob("*.py"):
            if path.name == "commits.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else ([node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
                )
                for target in targets:
                    self.assertNotIn(".state.", ast.unparse(target), str(path))
