"""Numeric boundary regressions, independent of Docker or model availability."""
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from optiprofiler_evolve.criteria.performance.numeric_proxy import NumericSolver
from optiprofiler_evolve.criteria.performance.numeric_runtime import pack, read_frame, write_frame


class NumericExceptionTests(unittest.TestCase):
    def test_async_docker_removal_is_verified_before_marking_cleanup_failed(self):
        from optiprofiler_evolve.criteria.performance.numeric_host import remove_numeric_container
        replies = [
            subprocess.CompletedProcess([], 1, "", "removal is already in progress"),
            subprocess.CompletedProcess([], 0, "removing", ""),
            subprocess.CompletedProcess([], 1, "", "error: no such object: solver-test"),
        ]
        with patch("subprocess.run", side_effect=replies) as run:
            receipt = remove_numeric_container("solver-test")
        self.assertEqual(receipt["status"], "removed")
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["rm", "inspect", "inspect"])

    def test_unresponsive_docker_cleanup_has_an_unconfirmed_receipt(self):
        from optiprofiler_evolve.config import EvaluationConfig
        from optiprofiler_evolve.criteria.performance.numeric_host import NumericEvaluationSession
        from optiprofiler_evolve.solver import InterfaceSpec
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = NumericEvaluationSession(root, InterfaceSpec.parse("solver.py:solver"),
                                               EvaluationConfig(), root / "runtime.py")
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 20)):
                result = session.run([sys.executable, "-c", "pass"], "scorer-test")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(session.cleanup_records[-1]["status"], "cleanup_unconfirmed")

    def test_unrelated_solver_error_does_not_replay_a_caught_oracle_exception(self):
        self.exchange(False, RuntimeError)

    def test_explicitly_propagated_oracle_error_preserves_the_original_exception(self):
        self.exchange(True, StopIteration)

    def exchange(self, propagate, expected):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "peer.sock")
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            errors = []
            def peer():
                try:
                    with server.accept()[0] as connection, connection.makefile("rwb") as stream:
                        read_frame(stream)
                        write_frame(stream, {"kind": "oracle", "index": 0, "x": pack([0.0])})
                        reply = read_frame(stream)
                        message = {"kind": "error"}
                        if propagate:
                            message["oracle_error_id"] = reply.get("oracle_error_id")
                        write_frame(stream, message)
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=peer)
            thread.start()
            def fun(_):
                raise StopIteration("real oracle budget")
            try:
                with self.assertRaises(expected):
                    NumericSolver(path, 5)(fun, [0.0])
            finally:
                thread.join(timeout=5)
                server.close()
            self.assertFalse(errors)
