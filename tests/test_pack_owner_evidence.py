from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.owner_views import render_owner_views

from test_owner_views import ATTEMPT, REVIEW_JOB, _build_run_dir

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pack_owner_evidence.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


class PackOwnerEvidenceTests(unittest.TestCase):
    def _prepared_run(self, root: Path) -> Path:
        run_dir = _build_run_dir(root)
        render_owner_views(run_dir / "events.jsonl", run_dir, final=True)
        return run_dir

    def test_requires_explicit_selection_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            result = _run(str(run_dir))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--attempt", result.stderr)
            result = _run(str(run_dir), "--all")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MANIFEST.json", result.stderr)

    def test_packs_one_attempt_with_relative_members_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._prepared_run(Path(directory))
            result = _run(str(run_dir), "--attempt", ATTEMPT)
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = run_dir / "owner" / "bundles" / f"{ATTEMPT}.tar.gz"
            self.assertTrue(bundle.is_file())
            with tarfile.open(bundle) as archive:
                members = archive.getmembers()
                names = [member.name for member in members]
            self.assertTrue(names)
            for member in members:
                self.assertFalse(member.issym() or member.islnk(), member.name)
            for name in names:
                self.assertFalse(name.startswith("/"), name)
                self.assertNotIn("..", name)
            self.assertTrue(
                any(name.startswith(f"transcripts/{ATTEMPT}") for name in names)
            )
            self.assertTrue(
                any(name.startswith(f"controller/integrity_reviews/{ATTEMPT}") for name in names)
            )
            self.assertFalse(any(name.startswith("public") for name in names))
            self.assertFalse(any("events.jsonl" == name for name in names))

    def test_packs_reviewer_job_and_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._prepared_run(Path(directory))
            result = _run(str(run_dir), "--role", REVIEW_JOB)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                (run_dir / "owner" / "bundles" / f"{REVIEW_JOB}.tar.gz").is_file()
            )
            result = _run(str(run_dir), "--all")
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(run_dir / "owner" / "bundles" / "owner_evidence.tar.gz") as archive:
                names = archive.getnames()
            self.assertTrue(
                any(name.startswith("research/transcripts/integrity-reviewer") for name in names)
            )

    def test_refuses_symlinks_inside_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._prepared_run(Path(directory))
            secret = Path(directory) / "outside-secret.txt"
            secret.write_text("OUTSIDE_SECRET", encoding="utf-8")
            (run_dir / "workspaces" / ATTEMPT / "escape").symlink_to(secret)
            result = _run(str(run_dir), "--attempt", ATTEMPT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Symlink", result.stderr)

    def test_refuses_traversal_absolute_and_run_dir_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._prepared_run(Path(directory))
            manifest_path = run_dir / "owner" / "MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for bad in ("../outside", "/etc", ".", "public/status.html"):
                manifest["attempts"][0]["evidence"] = {
                    "bad": {"path": bad, "bytes": 1}
                }
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                result = _run(str(run_dir), "--attempt", ATTEMPT)
                self.assertNotEqual(result.returncode, 0, bad)
                self.assertIn("refus", result.stderr.lower(), bad)

    def test_unknown_attempt_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._prepared_run(Path(directory))
            result = _run(str(run_dir), "--attempt", "nope")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not present", result.stderr)


if __name__ == "__main__":
    unittest.main()
