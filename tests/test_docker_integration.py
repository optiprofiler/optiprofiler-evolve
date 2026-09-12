from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

RUN_DOCKER = os.environ.get("OPE_RUN_DOCKER_TESTS") == "1"
GATEWAY_IMAGE = os.environ.get(
    "OPE_GATEWAY_IMAGE",
    "optiprofiler-evolve-gateway:latest",
)
EVALUATOR_IMAGE = os.environ.get(
    "OPE_EVALUATOR_IMAGE",
    "optiprofiler-evolve-evaluator:latest",
)


@unittest.skipUnless(RUN_DOCKER, "set OPE_RUN_DOCKER_TESTS=1 for Docker integration")
class DockerGatewayIntegrationTests(unittest.TestCase):
    def test_worker_has_gateway_transport_without_direct_egress_or_secret(self) -> None:
        suffix = uuid.uuid4().hex[:12]
        internal = f"ope-test-internal-{suffix}"
        egress = f"ope-test-egress-{suffix}"
        upstream = f"ope-test-upstream-{suffix}"
        gateway = f"ope-test-gateway-{suffix}"
        worker = f"ope-test-worker-{suffix}"
        secret = f"test-secret-{suffix}"
        objects = (worker, gateway, upstream)
        networks = (egress, internal)

        with tempfile.TemporaryDirectory(prefix=".docker-test-", dir=Path.cwd()) as directory:
            state = Path(directory) / "gateway"
            state.mkdir(mode=0o700)
            try:
                _run(["docker", "network", "create", "--internal", internal])
                _run(["docker", "network", "create", egress])
                _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        upstream,
                        "--network",
                        egress,
                        "--network-alias",
                        "fake-upstream",
                        "--entrypoint",
                        "python",
                        GATEWAY_IMAGE,
                        "-c",
                        _FAKE_UPSTREAM,
                    ]
                )
                _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        gateway,
                        "--network",
                        egress,
                        "--user",
                        f"{os.getuid()}:{os.getgid()}",
                        "--mount",
                        f"type=bind,src={state},dst=/state",
                        "--env",
                        f"TEST_PROVIDER_KEY={secret}",
                        GATEWAY_IMAGE,
                        "--listen",
                        "0.0.0.0:8080",
                        "--protocol",
                        "openai_responses",
                        "--upstream-base-url",
                        "http://fake-upstream:8000",
                        "--credential-env",
                        "TEST_PROVIDER_KEY",
                        "--auth-mode",
                        "bearer",
                        "--audit-log",
                        "/state/requests.jsonl",
                        "--ready-file",
                        "/state/ready.json",
                        "--outcome-file",
                        "/state/outcome.json",
                        "--advertised-base-url",
                        "http://provider-gateway:8080",
                        "--allow-private-upstream-for-tests",
                    ]
                )
                _run(
                    [
                        "docker",
                        "network",
                        "connect",
                        "--alias",
                        "provider-gateway",
                        internal,
                        gateway,
                    ]
                )
                _wait_for_file(state / "ready.json")
                _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        worker,
                        "--network",
                        internal,
                        "--env",
                        "OPENAI_API_KEY=dummy",
                        "--entrypoint",
                        "python",
                        GATEWAY_IMAGE,
                        "-c",
                        _WORKER_PROBE,
                    ]
                )
                _wait_for_log(worker, "probe-ok")

                worker_inspect = _inspect(worker)
                worker_env = worker_inspect["Config"]["Env"]
                worker_networks = worker_inspect["NetworkSettings"]["Networks"]
                self.assertNotIn(secret, json.dumps(worker_env))
                self.assertEqual(set(worker_networks), {internal})
                audit = (state / "requests.jsonl").read_text(encoding="utf-8")
                self.assertIn('"path": "/v1/models"', audit)
                self.assertIn('"outcome": "completed"', audit)
                self.assertNotIn(secret, audit)

                _run(["docker", "stop", "--time", "5", gateway])
                outcome = json.loads((state / "outcome.json").read_text(encoding="utf-8"))
                self.assertEqual(outcome["outcome"], "completed")
                self.assertEqual(outcome["request_count"], 1)
                self.assertEqual(outcome["inflight_request_count"], 0)
            finally:
                for name in objects:
                    _run(["docker", "rm", "-f", name], check=False)
                for name in networks:
                    _run(["docker", "network", "rm", name], check=False)

            for name in objects:
                self.assertNotEqual(
                    _run(["docker", "inspect", name], check=False).returncode,
                    0,
                )
            for name in networks:
                self.assertNotEqual(
                    _run(["docker", "network", "inspect", name], check=False).returncode,
                    0,
                )


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"Docker command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr.strip()}"
        )
    return completed


def _inspect(name: str) -> dict[str, object]:
    payload = json.loads(_run(["docker", "inspect", name]).stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise AssertionError(f"unexpected Docker inspect payload for {name}")
    result = payload[0]
    if not isinstance(result, dict):
        raise AssertionError(f"unexpected Docker inspect object for {name}")
    return result


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_log(container: str, marker: str) -> None:
    deadline = time.monotonic() + 15
    last = ""
    while time.monotonic() < deadline:
        completed = _run(["docker", "logs", container], check=False)
        last = completed.stdout + completed.stderr
        if marker in last:
            return
        state = _inspect(container)["State"]
        if isinstance(state, dict) and not state.get("Running"):
            break
        time.sleep(0.1)
    raise AssertionError(f"worker probe failed before {marker!r}: {last}")


_FAKE_UPSTREAM = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"data": []}\n'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""


_WORKER_PROBE = r"""
import json
import os
import socket
import time
import urllib.request

with urllib.request.urlopen('http://provider-gateway:8080/_optiprofiler/health', timeout=5) as r:
    assert json.load(r)['status'] == 'ok'
with urllib.request.urlopen('http://provider-gateway:8080/v1/models', timeout=5) as r:
    assert json.load(r)['data'] == []
assert 'TEST_PROVIDER_KEY' not in os.environ
try:
    socket.getaddrinfo('fake-upstream', 8000)
except socket.gaierror:
    pass
else:
    raise AssertionError('worker resolved the egress-only upstream')
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('worker reached unrelated external egress')
print('probe-ok', flush=True)
time.sleep(30)
"""


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(RUN_DOCKER, "set OPE_RUN_DOCKER_TESTS=1 for Docker integration")
class DockerEvaluatorIntegrationTests(unittest.TestCase):
    """Deterministic in-container scoring smoke: no model, no secrets."""

    def _source_evaluation(self, root, source, *, problems=("ROSENBR",), **overrides):
        from optiprofiler_evolve.config import EvaluationConfig
        from optiprofiler_evolve.criteria.performance.evaluator import DockerOptiProfilerEvaluator
        from optiprofiler_evolve.data import DataPlan
        from optiprofiler_evolve.solver import InterfaceSpec

        for name in ("candidate", "reference"):
            (root / name).mkdir()
            (root / name / "solver.py").write_text(source)
        data = DataPlan(
            library="s2mpj", selection={}, universe=problems, public=problems,
            validation=(), hidden=(), smoke=problems, split_seed=0,
            split_fingerprint="ci-numeric", aliases={p: f"P_{i}" for i, p in enumerate(problems)},
        )
        options = dict(backend="docker", docker_image=EVALUATOR_IMAGE,
                       timeout_seconds=120, cpus=2, memory="2g", pids_limit=256,
                       benchmark={"ptype": "ubln", "max_eval_factor": 2,
                                  "n_jobs": 2, "score_only": True})
        options.update(overrides)
        evaluator = DockerOptiProfilerEvaluator(
            reference=root / "reference", interface=InterfaceSpec.parse("solver.py:solver"),
            data=data, config=EvaluationConfig(**options),
        )
        return evaluator

    def test_numeric_boundary_preserves_ubln_budget_and_parallel_calls(self) -> None:
        source = """import numpy as np
def solver(fun, x0, *args):
    if len(args) == 8:
        assert args[-2](x0).ndim == 1
        assert args[-1](x0).ndim == 1
    for i in range(100):
        try:
            fun(x0 + i * 0.01)
        except StopIteration:
            return x0
    raise AssertionError('The objective evaluation limit was not enforced.')
"""
        with tempfile.TemporaryDirectory(prefix=".docker-ubln-", dir=Path.cwd()) as directory:
            root = Path(directory)
            evaluator = self._source_evaluation(
                root, source, problems=("ROSENBR", "HS5", "HS21", "HS23"),
            )
            result = evaluator.evaluate(root / "candidate", "smoke", root / "results")
            self.assertTrue(result.success, (root / "results/evaluator.log").read_text())
            self.assertAlmostEqual(result.score, 0.5)
            calls = json.loads((root / "results/numeric_calls.json").read_text())["calls"]
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(c["outcome"] == "returned" for c in calls), calls)
            # n=2, max_eval_factor=2: 8 calls then StopIteration on call 9.
            # Only the nonlinear case adds one cub and one ceq request.
            self.assertEqual(sorted(c["oracle_calls"] for c in calls), [9, 9, 9, 11])

    def test_candidate_cannot_read_scorer_files_or_reach_network(self) -> None:
        source = """def solver(fun, x0):
    from pathlib import Path
    import socket
    import importlib.util
    assert importlib.util.find_spec('optiprofiler') is None
    assert importlib.util.find_spec('optiprofiler_evolve') is None
    for name in ('/reference', '/output', '/request', '/problem-libraries', '/var/run/docker.sock'):
        assert not Path(name).exists(), name
    try:
        socket.create_connection(('1.1.1.1', 80), timeout=0.2)
    except OSError:
        pass
    else:
        raise AssertionError('Unexpected external network access.')
    fun(x0)
    return x0
"""
        with tempfile.TemporaryDirectory(prefix=".docker-private-", dir=Path.cwd()) as directory:
            root = Path(directory)
            evaluator = self._source_evaluation(root, source)
            (root / "reference/solver.py").write_text("def solver(fun, x0): fun(x0); return x0\n")
            result = evaluator.evaluate(root / "candidate", "smoke", root / "results")
            self.assertTrue(result.success, (root / "results/evaluator.log").read_text())
            self.assertAlmostEqual(result.score, 0.5)
            calls = json.loads((root / "results/numeric_calls.json").read_text())["calls"]
            self.assertEqual([(c["outcome"], c["oracle_calls"]) for c in calls], [("returned", 1)])

    def test_missing_solver_runtime_fails_closed_instead_of_scoring_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".docker-missing-", dir=Path.cwd()) as directory:
            root = Path(directory)
            evaluator = self._source_evaluation(
                root, "def solver(fun, x0): return x0\n",
                solver_image="ope-nonexistent-numeric-runtime:negative-control",
            )
            result = evaluator.evaluate(root / "candidate", "smoke", root / "results")
            self.assertFalse(result.success)

    def test_solver_timeout_cleans_up_scorer_and_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".docker-timeout-", dir=Path.cwd()) as directory:
            root = Path(directory)
            evaluator = self._source_evaluation(
                root, "def solver(fun, x0):\n    import time\n    time.sleep(120)\n    return x0\n",
                timeout_seconds=10,
            )
            (root / "reference/solver.py").write_text("def solver(fun, x0): return x0\n")
            before = set(_run(["docker", "ps", "-aq", "--filter", "name=ope-solver-"]).stdout.split())
            started = time.monotonic()
            result = evaluator.evaluate(root / "candidate", "smoke", root / "results")
            self.assertFalse(result.success)
            self.assertIn("124", result.error)
            self.assertLess(time.monotonic() - started, 40)
            after = set(_run(["docker", "ps", "-aq", "--filter", "name=ope-solver-"]).stdout.split())
            self.assertEqual(after - before, set())

    def test_malformed_output_or_killed_solver_never_produces_a_success_score(self) -> None:
        sources = (
            "def solver(fun, x0):\n    import os\n    os._exit(42)\n",
            "def solver(fun, x0):\n    import os\n    os.write(3, b'not-json\\n')\n    return x0\n",
        )
        for source in sources:
            with self.subTest(source=source), tempfile.TemporaryDirectory(
                prefix=".docker-protocol-", dir=Path.cwd()
            ) as directory:
                root = Path(directory)
                evaluator = self._source_evaluation(root, source)
                (root / "reference/solver.py").write_text("def solver(fun, x0): return x0\n")
                result = evaluator.evaluate(root / "candidate", "smoke", root / "results")
                self.assertFalse(result.success)
                calls = json.loads((root / "results/numeric_calls.json").read_text())["calls"]
                self.assertTrue(calls)
                self.assertTrue(all(c["outcome"] == "disconnected" for c in calls))

    def test_candidate_cannot_replace_the_trusted_benchmark(self) -> None:
        from optiprofiler_evolve.config import EvaluationConfig
        from optiprofiler_evolve.criteria.performance.evaluator import DockerOptiProfilerEvaluator
        from optiprofiler_evolve.data import DataPlan
        from optiprofiler_evolve.solver import InterfaceSpec

        with tempfile.TemporaryDirectory(prefix=".docker-tamper-", dir=Path.cwd()) as directory:
            root = Path(directory)
            for name in ("candidate", "reference"):
                (root / name).mkdir()
            (root / "reference/solver.py").write_text("def solver(fun, x0): return x0\n")
            (root / "candidate/solver.py").write_text(
                "try:\n"
                "    import optiprofiler\n"
                "except ImportError:\n"
                "    pass\n"
                "else:\n"
                "    optiprofiler.benchmark = lambda *a, **kw: ([1., 0.], None, None)\n"
                "def solver(fun, x0): return x0\n"
            )
            data = DataPlan(
                library="s2mpj", selection={}, universe=("ROSENBR",),
                public=("ROSENBR",), validation=(), hidden=(), smoke=("ROSENBR",),
                split_seed=0, split_fingerprint="ci-tamper-negative",
                aliases={"ROSENBR": "P_OPAQUE_CI"},
            )
            evaluator = DockerOptiProfilerEvaluator(
                reference=root / "reference", interface=InterfaceSpec.parse("solver.py:solver"),
                data=data, config=EvaluationConfig(
                    backend="docker", docker_image=EVALUATOR_IMAGE, timeout_seconds=120,
                    cpus=1, memory="2g", pids_limit=256,
                    benchmark={"max_eval_factor": 5, "n_jobs": 1, "score_only": True},
                ),
            )
            result = evaluator.evaluate(root / "candidate", "smoke", root / "results")
            self.assertTrue(result.success, f"{result.error}\n{(root / 'results/evaluator.log').read_text()}")
            # Both solvers return x0. Candidate-side imports must not forge a win.
            self.assertAlmostEqual(result.score, 0.5, places=6)
            calls = json.loads((root / "results/numeric_calls.json").read_text())["calls"]
            self.assertTrue(calls)
            self.assertTrue(all(call["outcome"] == "returned" for call in calls), calls)

    def test_evaluator_scores_a_seed_candidate_in_container(self) -> None:
        from optiprofiler_evolve.config import EvaluationConfig
        from optiprofiler_evolve.criteria.performance.evaluator import DockerOptiProfilerEvaluator
        from optiprofiler_evolve.data import DataPlan
        from optiprofiler_evolve.solver import InterfaceSpec

        solver_source = (
            "import numpy as np\n\n\n"
            "def solver(fun, x0):\n"
            "    x = np.asarray(x0, dtype=float).copy()\n"
            "    value = fun(x)\n"
            "    step = np.maximum(1.0, np.abs(x))\n"
            "    for _ in range(60):\n"
            "        improved = False\n"
            "        for index in range(x.size):\n"
            "            for sign in (-1.0, 1.0):\n"
            "                trial = x.copy()\n"
            "                trial[index] += sign * step[index]\n"
            "                trial_value = fun(trial)\n"
            "                if trial_value < value:\n"
            "                    x, value = trial, trial_value\n"
            "                    improved = True\n"
            "        if not improved:\n"
            "            step *= 0.5\n"
            "    return x\n"
        )
        with tempfile.TemporaryDirectory(prefix=".docker-eval-", dir=Path.cwd()) as directory:
            root = Path(directory)
            for name in ("candidate", "reference"):
                (root / name).mkdir()
                (root / name / "solver.py").write_text(solver_source, encoding="utf-8")
            data = DataPlan(
                library="s2mpj",
                selection={},
                universe=("ROSENBR",),
                public=("ROSENBR",),
                validation=(),
                hidden=(),
                smoke=("ROSENBR",),
                split_seed=0,
                split_fingerprint="ci-smoke",
                aliases={"ROSENBR": "P_OPAQUE_CI"},
            )
            evaluator = DockerOptiProfilerEvaluator(
                reference=root / "reference",
                interface=InterfaceSpec.parse("solver.py:solver"),
                data=data,
                config=EvaluationConfig(
                    backend="docker",
                    docker_image=EVALUATOR_IMAGE,
                    timeout_seconds=600,
                    cpus=1,
                    memory="2g",
                    pids_limit=256,
                    benchmark={"max_eval_factor": 5, "n_jobs": 1, "score_only": True},
                ),
            )
            output_dir = root / "controller" / "evaluations" / "seed" / "smoke"
            result = evaluator.evaluate(root / "candidate", "smoke", output_dir)

            self.assertTrue(result.success, result.error)
            self.assertEqual(result.problem_count, 1)
            self.assertAlmostEqual(result.score, 0.5, places=6)
            self.assertTrue((output_dir / "result.json").is_file())
            log = (output_dir / "evaluator.log").read_text(encoding="utf-8")
            self.assertNotIn("ROSENBR", log)
            self.assertNotIn(
                "ROSENBR", (output_dir / "result.json").read_text(encoding="utf-8")
            )
            # The request staging never lingers next to the output tree.
            leftovers = list(output_dir.parent.glob(".ope-evaluator-request-*"))
            self.assertEqual(leftovers, [])
