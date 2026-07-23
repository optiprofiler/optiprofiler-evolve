from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "examples" / "github-actions" / "evolve.yml"


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
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("workflow_dispatch:", text)


if __name__ == "__main__":
    unittest.main()
