from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from optiprofiler_evolve.serve import DashboardError, prepare


def _run_dir(root: Path) -> Path:
    run_dir = root / "runs" / "demo"
    (run_dir / "public").mkdir(parents=True)
    (run_dir / "status.html").write_text("<!doctype html>OWNER", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "public" / "status.html").write_text(
        "<!doctype html>PUBLIC", encoding="utf-8"
    )
    (root / "runs" / "sibling-secret.txt").write_text(
        "PARENT_SECRET_991", encoding="utf-8"
    )
    return run_dir


def _get(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path)
    response = connection.getresponse()
    payload = response.read()
    status = response.status
    connection.close()
    return status, payload


class ServeDashboardTests(unittest.TestCase):
    def _serving(self, run_dir: Path, **kwargs):
        server, url, warnings = prepare(run_dir, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, url, warnings

    def test_owner_mode_defaults_to_loopback_and_prints_real_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _run_dir(Path(directory))
            server, url, warnings = self._serving(run_dir)

            host, port = server.server_address[:2]
            self.assertEqual(str(host), "127.0.0.1")
            self.assertNotEqual(port, 0)
            self.assertEqual(url, f"http://127.0.0.1:{port}/status.html")
            self.assertEqual(warnings, [])

            status, payload = _get(port, "/status.html")
            self.assertEqual(status, 200)
            self.assertIn(b"OWNER", payload)
            status, payload = _get(port, "/events.jsonl")
            self.assertEqual(status, 200)

    def test_public_mode_serves_only_the_sanitized_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _run_dir(Path(directory))
            server, url, _warnings = self._serving(run_dir, public=True)
            port = server.server_address[1]

            self.assertEqual(url, f"http://127.0.0.1:{port}/status.html")
            status, payload = _get(port, "/status.html")
            self.assertEqual(status, 200)
            self.assertIn(b"PUBLIC", payload)
            self.assertNotIn(b"OWNER", payload)
            # Private run files are outside the served root.
            self.assertEqual(_get(port, "/events.jsonl")[0], 404)
            self.assertEqual(_get(port, "/../events.jsonl")[0], 404)

    def test_served_root_never_reaches_the_runs_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _run_dir(Path(directory))
            server, _url, _warnings = self._serving(run_dir)
            port = server.server_address[1]

            for path in (
                "/../sibling-secret.txt",
                "/../../runs/sibling-secret.txt",
                "/%2e%2e/sibling-secret.txt",
                "/..%2fsibling-secret.txt",
            ):
                status, payload = _get(port, path)
                self.assertNotEqual(status, 200, path)
                self.assertNotIn(b"PARENT_SECRET_991", payload, path)

    def test_non_loopback_bind_requires_explicit_host_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = _run_dir(Path(directory))
            server, _url, warnings = self._serving(run_dir, host="0.0.0.0")
            self.assertEqual(len(warnings), 1)
            self.assertIn("PRIVATE owner evidence", warnings[0])

            _server, _url, public_warnings = self._serving(
                run_dir, public=True, host="0.0.0.0"
            )
            self.assertIn("sanitized public bundle", public_warnings[0])

    def test_missing_dashboards_raise_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            with self.assertRaises(DashboardError) as owner_error:
                prepare(empty)
            self.assertIn("No owner dashboard", str(owner_error.exception))
            with self.assertRaises(DashboardError) as public_error:
                prepare(empty, public=True)
            self.assertIn("No sanitized dashboard", str(public_error.exception))
            with self.assertRaises(DashboardError):
                prepare(root / "missing")


if __name__ == "__main__":
    unittest.main()
