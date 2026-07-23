from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "examples" / "github-actions" / "evolve.yml"
EXTERNAL_ROOT = ROOT / "examples" / "external-repository"
EXTERNAL_WORKFLOW = EXTERNAL_ROOT / "evolve.yml"


class GitHubActionsTemplateTests(unittest.TestCase):
    def test_template_is_one_job_and_uploads_only_public_bundle(self) -> None:
        payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        jobs = payload["jobs"]
        self.assertEqual(set(jobs), {"evolve"})
        steps = jobs["evolve"]["steps"]
        upload = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        )
        self.assertEqual(
            upload["with"]["path"],
            "${{ env.OPTIPROFILER_EVOLVE_RUN_DIR }}/public",
        )
        self.assertEqual(upload["if"], "${{ always() }}")
        self.assertFalse(upload["with"]["include-hidden-files"])

    def test_template_summary_never_reads_private_report_or_run_root(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("public/PUBLIC_REPORT.md", text)
        self.assertNotIn("FINAL_REPORT.md", text)
        self.assertNotIn("path: ${{ env.OPTIPROFILER_EVOLVE_RUN_DIR }}\n", text)
        self.assertNotIn("owner/", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("workflow_dispatch:", text)

    def test_external_repository_template_fetches_package_outside_solver_tree(self) -> None:
        payload = yaml.safe_load(EXTERNAL_WORKFLOW.read_text(encoding="utf-8"))
        job = payload["jobs"]["evolve"]
        self.assertEqual(set(payload["jobs"]), {"evolve"})
        self.assertEqual(
            job["env"]["OPTIPROFILER_EVOLVE_SOURCE"],
            "${{ runner.temp }}/optiprofiler-evolve",
        )
        text = EXTERNAL_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("https://github.com/optiprofiler/optiprofiler-evolve.git", text)
        self.assertIn('fetch --depth 1 origin \\\n            "$OPTIPROFILER_EVOLVE_REF"', text)
        self.assertIn('python -m pip install "$OPTIPROFILER_EVOLVE_SOURCE"', text)
        self.assertIn('"$OPTIPROFILER_EVOLVE_SOURCE/docker/worker/Dockerfile"', text)
        self.assertIn("run: python evolve/run.py", text)

    def test_external_repository_template_uploads_only_public_bundle(self) -> None:
        payload = yaml.safe_load(EXTERNAL_WORKFLOW.read_text(encoding="utf-8"))
        steps = payload["jobs"]["evolve"]["steps"]
        uploads = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        ]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(
            uploads[0]["with"]["path"],
            "${{ env.OPTIPROFILER_EVOLVE_RUN_DIR }}/public",
        )
        text = EXTERNAL_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("FINAL_REPORT.md", text)
        self.assertNotIn("status_private.html", text)
        self.assertNotIn("owner/", text)

    def test_external_repository_launcher_targets_only_solver_directory(self) -> None:
        launcher = (EXTERNAL_ROOT / "evolve" / "run.py").read_text(encoding="utf-8")
        compile(launcher, str(EXTERNAL_ROOT / "evolve" / "run.py"), "exec")
        self.assertIn("parents[1]", launcher)
        self.assertIn('initial=ROOT / "solver"', launcher)
        self.assertIn('interface="solver.py:solver"', launcher)
        self.assertTrue((EXTERNAL_ROOT / "solver" / "solver.py").is_file())


if __name__ == "__main__":
    unittest.main()
