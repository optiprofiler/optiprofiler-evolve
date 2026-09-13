from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_config import minimal_config

from optiprofiler_evolve.agents.broker import BrokerConnection
from optiprofiler_evolve.agents.contracts import WorkerRequest
from optiprofiler_evolve.config import load_config


def harbor_config() -> dict:
    raw = minimal_config()
    raw["workers"].update({
        "adapter": "harbor",
        "pool": [{
            "harness": "claude",
            "model": "synthetic-model",
            "env": {"ANTHROPIC_API_KEY": "synthetic-secret"},
            "provider_gateway": {
                "upstream_base_url": "https://api.anthropic.com",
                "credential_env": "ANTHROPIC_API_KEY",
                "auth_mode": "x-api-key",
            },
        }],
    })
    raw["sandbox"]["backend"] = "docker"
    return raw


class HarborConfigurationTests(unittest.TestCase):
    def test_subscription_mount_is_read_only_and_retry_preserves_launch_evidence(self):
        from optiprofiler_evolve.agents.harbor import HarborInvocationRuntime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            auth = root / "auth.json"
            auth.write_text('{"fake": "test-only"}')
            (root / "invocation.json").write_text('{"trace_id":"subscription-retry","run_id":"test"}')
            raw = harbor_config()
            raw["workers"].update({"allow_direct_provider": True,
                "pool": [{"harness": "codex", "model": "fake", "env": {"CODEX_AUTH_JSON_PATH": str(auth)}}]})
            raw["sandbox"]["allow_direct_network"] = True
            config = load_config(raw)
            runtime = HarborInvocationRuntime(worker=config.workers.pool[0], workers=config.workers,
                sandbox=config.sandbox, selected_values=dict(config.workers.pool[0].env), trace_dir=root)
            args = dict(workspace=root, tools_dir=root,
                broker=BrokerConnection("/broker", "/artifacts", "fake", root, root),
                harness=["codex", "exec"], selected_values=dict(config.workers.pool[0].env))
            first = Path(runtime.worker_command(**args)[-1])
            before = first.read_bytes()
            second = Path(runtime.worker_command(**args)[-1])
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), before)
            launch = json.loads(before)
            mount = next(m for m in launch["container"]["mounts"] if m["target"] == "/run/ope-codex-auth.json")
            self.assertTrue(mount["readonly"])
            self.assertNotIn("test-only", before.decode())

    def test_harbor_refuses_to_map_a_root_controller_into_the_worker(self) -> None:
        from optiprofiler_evolve.agents.harbor import HarborWorkerAdapter

        with patch("os.geteuid", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "non-root controller"):
                HarborWorkerAdapter()

    def test_harbor_uses_the_existing_gateway_contract(self) -> None:
        config = load_config(harbor_config())
        self.assertEqual(config.workers.adapter, "harbor")
        self.assertEqual(config.sandbox.backend, "docker")
        self.assertIsNotNone(config.workers.pool[0].provider_gateway)
        self.assertNotIn("synthetic-secret", str(config.redacted_dict()))

    def test_harbor_never_falls_back_to_unsafe_local_or_implicit_direct_network(self) -> None:
        raw = harbor_config()
        raw["sandbox"]["backend"] = "unsafe_local"
        raw["workers"]["pool"][0].pop("provider_gateway")
        with self.assertRaisesRegex(ValueError, "Harbor requires"):
            load_config(raw)

    def test_harbor_subscription_requires_explicit_path_and_direct_opt_in(self) -> None:
        raw = harbor_config()
        raw["workers"]["pool"] = [{"harness": "codex", "model": "gpt-5.3-codex-spark",
                                  "env": {"CODEX_AUTH_JSON_PATH": "/owner/auth.json"}}]
        with self.assertRaises(ValueError):
            load_config(raw)
        raw["workers"]["allow_direct_provider"] = True
        raw["sandbox"]["allow_direct_network"] = True
        self.assertEqual(load_config(raw).workers.adapter, "harbor")
        raw["workers"]["pool"][0]["env"] = {}
        raw["sandbox"]["backend"] = "docker"
        raw["workers"]["allow_direct_provider"] = True
        raw["sandbox"]["allow_direct_network"] = True
        with self.assertRaisesRegex(ValueError, "Harbor requires"):
            load_config(raw)

    def test_harbor_codex_preserves_the_no_shell_restriction(self) -> None:
        raw = harbor_config()
        raw["workers"]["pool"][0]["harness"] = "codex"
        raw["workers"]["pool"][0]["provider_gateway"]["upstream_base_url"] = (
            "https://api.openai.com/v1"
        )
        raw["workers"]["tools"] = {"shell": False}
        with self.assertRaisesRegex(ValueError, "shell=false"):
            load_config(raw)


@unittest.skipUnless(os.environ.get("OPE_RUN_HARBOR_TESTS") == "1", "Harbor Docker opt-in")
class HarborExecutionTests(unittest.TestCase):
    @contextmanager
    def invocation(self, body: str, *, timeout: int = 120, cancellation=None):
        # A synthetic CLI tests the real container and transport without API quota.
        parent = Path(os.environ.get("OPE_TEST_ROOT", Path.cwd()))
        with tempfile.TemporaryDirectory(prefix="ope-harbor-test-", dir=parent) as directory:
            root = Path(directory).resolve()
            for name in ("workspace", "tools", "queue", "artifacts"):
                (root / name).mkdir()
            executable = root / "tools" / "claude"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, socket, sys, time\n"
                "assert sys.stdin.read() == 'synthetic prompt'\n"
                "assert not pathlib.Path('/var/run/docker.sock').exists()\n"
                "assert not pathlib.Path('/reference').exists()\n"
                "assert os.environ['ANTHROPIC_API_KEY'] != 'synthetic-secret'\n"
                + body,
                encoding="utf-8",
            )
            executable.chmod(0o755)
            config = load_config(harbor_config())
            sandbox = replace(
                config.sandbox,
                worker_image=os.environ.get("OPE_HARBOR_TEST_IMAGE", "python:3.12-slim-bookworm"),
                gateway_image=os.environ.get("OPE_GATEWAY_IMAGE", "optiprofiler-evolve-gateway:latest"),
            )
            request = WorkerRequest(
                worker=config.workers.pool[0],
                workers=replace(config.workers, timeout_seconds=timeout),
                sandbox=sandbox,
                workspace=root / "workspace", tools_dir=root / "tools",
                broker=BrokerConnection("/opt/broker", "/opt/artifacts", "synthetic-token",
                                        root / "queue", root / "artifacts"),
                prompt="synthetic prompt", transcript=root / "transcript.txt",
                trace_dir=root / "trace",
                cancellation_event=cancellation,
            )
            yield request, root

    def assert_clean(self, root: Path) -> None:
        cleanup = json.loads((root / "trace/docker_cleanup.json").read_text())
        self.assertTrue(cleanup["complete"], cleanup)
        inspection = json.loads((root / "trace/harbor/container_inspect.json").read_text())
        remaining = subprocess.run(
            ["docker", "inspect", inspection["Id"]], capture_output=True, check=False,
        )
        self.assertNotEqual(remaining.returncode, 0)

    def test_real_harbor_container_preserves_output_on_nonzero_cli_exit(self) -> None:
        from optiprofiler_evolve.agents.harbor import HarborWorkerAdapter

        body = (
            "pathlib.Path('solver.py').write_text('def solver(fun, x0, **kw): return x0\\n')\n"
            "os.write(1, b'native-out\\x00\\xff\\n')\n"
            "os.write(2, b'native-err\\x00\\xfe\\n')\n"
            "raise SystemExit(7)\n"
        )
        with self.invocation(body) as (request, root):
            result = HarborWorkerAdapter().run(request)
            self.assertEqual(result.returncode, 7, result.transcript.read_text())
            self.assertTrue((root / "workspace" / "solver.py").is_file())
            self.assertIn(b"native-out\x00\xff\n", result.native_trace.read_bytes())
            self.assertIn(b"native-err\x00\xfe\n", result.stderr_trace.read_bytes())
            inspection = json.loads((root / "trace/harbor/container_inspect.json").read_text())
            self.assertTrue(inspection["HostConfig"]["ReadonlyRootfs"])
            self.assertEqual(len(inspection["NetworkSettings"]["Networks"]), 1)
            self.assertNotIn("synthetic-secret", json.dumps(inspection))
            self.assert_clean(root)

    def test_real_worker_cannot_write_tools_or_use_direct_egress(self) -> None:
        from optiprofiler_evolve.agents.harbor import HarborWorkerAdapter

        body = (
            "for path in ('/opt/optiprofiler-evolve/tools/escape', '/opt/artifacts/escape', '/etc/escape'):\n"
            "    try: pathlib.Path(path).write_text('escape')\n"
            "    except OSError: pass\n"
            "    else: raise AssertionError('writable: ' + path)\n"
            "try: socket.create_connection(('1.1.1.1', 80), timeout=1)\n"
            "except OSError: pass\n"
            "else: raise AssertionError('direct egress available')\n"
            "print('isolation-ok', flush=True)\n"
        )
        with self.invocation(body) as (request, root):
            result = HarborWorkerAdapter().run(request)
            self.assertEqual(result.returncode, 0, result.transcript.read_text())
            self.assertIn(b'isolation-ok', result.native_trace.read_bytes())
            self.assert_clean(root)

    def test_timeout_stops_the_container_not_only_docker_exec(self) -> None:
        from optiprofiler_evolve.agents.harbor import HarborWorkerAdapter

        with self.invocation("print('before-timeout', flush=True)\ntime.sleep(120)\n", timeout=15) as (request, root):
            result = HarborWorkerAdapter().run(request)
            self.assertTrue(result.timed_out, result)
            self.assertIn(b'before-timeout', result.native_trace.read_bytes())
            self.assert_clean(root)

    def test_cancel_stops_the_container_and_keeps_partial_output(self) -> None:
        from optiprofiler_evolve.agents.harbor import HarborWorkerAdapter

        cancel = threading.Event()
        body = "pathlib.Path('ready').touch()\nprint('before-cancel', flush=True)\ntime.sleep(120)\n"
        with self.invocation(body, cancellation=cancel) as (request, root):
            def stop_when_ready():
                deadline = time.monotonic() + 30
                while not (root / 'workspace/ready').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                cancel.set()
            monitor = threading.Thread(target=stop_when_ready)
            monitor.start()
            try:
                result = HarborWorkerAdapter().run(request)
            finally:
                cancel.set()
                monitor.join(timeout=35)
            self.assertTrue(result.cancelled, result)
            self.assertIn(b'before-cancel', result.native_trace.read_bytes())
            self.assert_clean(root)
