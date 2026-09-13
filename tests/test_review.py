from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.criteria.integrity.reviewer import (
    REQUIRED_CHECKS,
    IntegrityReviewRequest,
    _review_prompt,
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
    def test_quarantine_quote_must_match_the_claimed_source_not_reversed_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "candidate").mkdir()
            (root / "candidate/solver.py").write_text("def solver(fun, x0):\n    return fun(x0)\n")
            evidence = {"path": "candidate/solver.py", "line": 2,
                        "detail": "claims no objective calls", "quote": "return x0"}
            report = root / "review.json"
            payload = _payload("quarantine", [{"category": "suspicious_shortcuts",
                "severity": "high", "summary": "unsupported claim", "evidence": [evidence]}])
            report.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "evidence-backed"):
                parse_review(report, root, require_source_quote=True)
            evidence["quote"] = "return fun(x0)"
            report.write_text(json.dumps(payload))
            self.assertEqual(parse_review(report, root, require_source_quote=True).verdict, "quarantine")

    def test_reviewer_prioritizes_source_diff_over_full_transcript_replay(self) -> None:
        request = IntegrityReviewRequest(
            candidate_id="candidate",
            candidate=Path("candidate"),
            parent=Path("parent"),
            changed_files=("solver.py",),
            interface="solver.py:solver",
            editable=("solver.py",),
            mutation_transcript=Path("mutation_transcript.txt"),
            reviewer_worker=None,
            review_attempt=1,
            timeout_seconds=60,
            token_budget=1000,
            max_budget_usd=None,
            run_agent=lambda _job: None,  # type: ignore[arg-type,return-value]
        )

        prompt = _review_prompt(request)

        self.assertIn("Prioritize the actual source diff", prompt)
        self.assertIn(
            "rather than reading the entire file sequentially",
            " ".join(prompt.split()),
        )
        self.assertIn("Write `review.json` as soon", prompt)
        self.assertIn("create the file `review.json` in your working directory", prompt)
        self.assertIn("JSON in the final chat response is not a submitted review", prompt)
        self.assertIn("Verify that the file exists and parses as JSON", prompt)

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
