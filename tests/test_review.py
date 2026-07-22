from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.review import (
    REQUIRED_CHECKS,
    parse_review,
    write_sanitized_transcript,
)


def _payload(verdict: str = "approve", findings: list[dict] | None = None) -> dict:
    return {
        "schema": "integrity_review/1",
        "verdict": verdict,
        "checked": sorted(REQUIRED_CHECKS),
        "summary": "review complete",
        "findings": findings or [],
    }


class ReviewParserTests(unittest.TestCase):
    def test_approve_may_have_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "candidate").mkdir()
            report = root / "review.json"
            report.write_text(json.dumps(_payload()), encoding="utf-8")
            decision = parse_review(report, root)
            self.assertEqual(decision.verdict, "approve")
            self.assertEqual(decision.findings, ())

    def test_quarantine_requires_existing_candidate_or_parent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "candidate").mkdir()
            (root / "candidate" / "solver.py").write_text("x = 1\n", encoding="utf-8")
            report = root / "review.json"
            report.write_text(
                json.dumps(
                    _payload(
                        "quarantine",
                        [
                            {
                                "category": "problem_hardcoding",
                                "severity": "high",
                                "summary": "case-specific branch",
                                "evidence": [
                                    {
                                        "path": "candidate/solver.py",
                                        "line": 1,
                                        "detail": "literal case dispatch",
                                    }
                                ],
                            }
                        ],
                    )
                ),
                encoding="utf-8",
            )
            decision = parse_review(report, root)
            self.assertEqual(decision.verdict, "quarantine")
            self.assertEqual(len(decision.findings), 1)

            payload = _payload(
                "quarantine",
                [
                    {
                        "category": "problem_hardcoding",
                        "severity": "high",
                        "summary": "unsupported allegation",
                        "evidence": [{"path": "candidate/missing.py", "detail": "missing"}],
                    }
                ],
            )
            report.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence-backed"):
                parse_review(report, root)

    def test_missing_required_check_and_unknown_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "review.json"
            payload = _payload()
            payload["checked"].remove("evaluation_gaming")
            report.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "omitted required checks"):
                parse_review(report, root)
            payload = _payload()
            payload["extra"] = True
            report.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown keys"):
                parse_review(report, root)

    def test_mutation_transcript_copy_redacts_configured_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            destination = root / "review.txt"
            source.write_text("used secret-token during public evaluation", encoding="utf-8")
            write_sanitized_transcript(
                source,
                destination,
                secret_values={"API_KEY": "secret-token"},
            )
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "used <redacted> during public evaluation",
            )


if __name__ == "__main__":
    unittest.main()
