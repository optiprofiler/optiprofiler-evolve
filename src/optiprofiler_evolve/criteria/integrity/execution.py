"""Private review retries and evidence, with no candidate-admission authority."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

from optiprofiler_evolve.config import (
    ComponentConfig,
    IntegrityReviewConfig,
    WorkerConfig,
    WorkersConfig,
    plain_data,
)
from optiprofiler_evolve.criteria.integrity.reviewer import (
    CandidateReviewer,
    IntegrityReviewDecision,
    IntegrityReviewRequest,
    write_private_review,
    write_sanitized_transcript,
)
from optiprofiler_evolve.errors import RunCancelled as _RunCancelled
from optiprofiler_evolve.files import write_json
from optiprofiler_evolve.solver import InterfaceSpec, tree_hash


class IntegrityReviewExecutor:
    def __init__(self, *, run_dir: Path, interface: InterfaceSpec, editable: tuple[str, ...],
                 workers: WorkersConfig, reviewer_config: Callable[[str], IntegrityReviewConfig],
                 reviewer_factory: Callable[[ComponentConfig], CandidateReviewer],
                 run_agent: Callable, emit: Callable) -> None:
        self.run_dir = run_dir
        self.interface = interface
        self.editable = editable
        self.workers = workers
        self.reviewer_config = reviewer_config
        self.reviewer_factory = reviewer_factory
        self.run_agent = run_agent
        self.emit = emit
        self.cache: dict[str, IntegrityReviewDecision] = {}
        self.failures: dict[str, str] = {}

    def review(
        self,
        *,
        candidate_id: str,
        candidate: Path,
        parent: Path,
        changed: tuple[str, ...],
        mutation_transcript: Path | None,
        mutation_worker: WorkerConfig | None,
        profile: str = "validation",
    ) -> tuple[IntegrityReviewDecision, Path]:
        """Obtain a review for exactly these source, transcript, and option inputs."""

        config = self.reviewer_config(profile)
        candidate_hash = tree_hash(candidate)
        parent_hash = tree_hash(parent)
        fingerprint = hashlib.sha256(json.dumps({
            "candidate": candidate_hash, "parent": parent_hash,
            "transcript": mutation_transcript.read_text() if mutation_transcript else None,
            "config": plain_data(config),
            "profile": profile,
        }, sort_keys=True, default=str).encode()).hexdigest()
        cache_key = f"{candidate_id}-{profile}-{fingerprint[:16]}"
        cached = self.cache.get(cache_key)
        normalized_root = self.run_dir / "controller" / "integrity_reviews" / cache_key
        if cached is not None:
            return cached, normalized_root / "decision.json"
        if cache_key in self.failures:
            raise RuntimeError(self.failures[cache_key])

        review_scope = {
            "phase": "prepare" if candidate_id == "seed" else "explore",
            **({"candidate_id": candidate_id} if candidate_id == "seed" else {"attempt_id": candidate_id}),
            "role": "integrity-reviewer",
        }
        # A candidate identical to its parent with no mutation transcript is the
        # fixed initial baseline (the seed), not a worker proposal: there is no
        # diff or transcript for a reviewer to audit, so approval is
        # definitional. Record the same private decision without spending
        # reviewer budget or letting a reviewer outage abort prepare.
        if candidate_hash == parent_hash and mutation_transcript is None:
            self.emit("integrity_review_started", "running", scope=review_scope)
            decision = IntegrityReviewDecision(
                verdict="approve",
                checked=(),
                summary=(
                    "Initial baseline: the candidate equals its parent and no "
                    "mutation transcript exists, so there is nothing to review."
                ),
                reason="baseline",
            )
            write_private_review(normalized_root / "decision.json", decision)
            self.cache[cache_key] = decision
            self.emit(
                "integrity_review_finished",
                "succeeded",
                scope=review_scope,
                data={"gate": "approved", "reason": "baseline", "profile": profile,
                      "report": str(normalized_root / "decision.json")},
            )
            return decision, normalized_root / "decision.json"

        transcript = normalized_root / "inputs" / "mutation_transcript.txt"
        secret_values: dict[str, str] = {}
        if mutation_worker is not None:
            secret_values.update(mutation_worker.env)
            secret_values.update(
                {name: os.environ[name] for name in mutation_worker.pass_env if name in os.environ}
            )
        if mutation_transcript is None:
            mutation_transcript = normalized_root / "inputs" / "empty_transcript.txt"
            mutation_transcript.parent.mkdir(parents=True, exist_ok=True)
            mutation_transcript.write_text(
                "[controller] no mutation transcript exists for this materialized variant.\n",
                encoding="utf-8",
            )
            mutation_transcript.chmod(0o600)
        write_sanitized_transcript(
            mutation_transcript,
            transcript,
            secret_values=secret_values,
        )
        reviewer = self.reviewer_factory(config.component)
        reviewer_worker = config.resolved_worker(self.workers)
        failures: list[str] = []
        self.emit("integrity_review_started", "running", scope=review_scope)
        for review_attempt in range(1, config.retries + 2):
            scope = {
                **review_scope,
                "job_id": f"{cache_key}-r{review_attempt:02d}",
            }
            self.emit(
                "integrity_review_attempt_started",
                "running",
                scope=scope,
                data={"review_attempt": review_attempt},
            )
            try:
                decision = reviewer.review(
                    IntegrityReviewRequest(
                        candidate_id=candidate_id,
                        candidate=candidate,
                        parent=parent,
                        changed_files=changed,
                        interface=f"{self.interface.file}:{self.interface.function}",
                        editable=self.editable,
                        mutation_transcript=transcript,
                        reviewer_worker=reviewer_worker,
                        review_attempt=review_attempt,
                        timeout_seconds=config.timeout_seconds,
                        token_budget=config.token_budget,
                        max_budget_usd=config.max_budget_usd,
                        run_agent=self.run_agent,
                        input_fingerprint=f"{profile}-{fingerprint[:16]}",
                    )
                )
            except _RunCancelled:
                raise
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                failures.append(failure)
                self.emit(
                    "integrity_review_attempt_finished",
                    "failed",
                    scope=scope,
                    data={
                        "gate": "unavailable",
                        "review_attempt": review_attempt,
                        "error": failure,
                    },
                )
                continue
            attempt_report = normalized_root / f"attempt_{review_attempt:02d}.json"
            write_private_review(attempt_report, decision)
            write_private_review(normalized_root / "decision.json", decision)
            self.cache[cache_key] = decision
            self.emit(
                "integrity_review_attempt_finished",
                "succeeded",
                scope=scope,
                data={
                    "review_attempt": review_attempt,
                    "verdict": decision.verdict,
                    "checked": list(decision.checked),
                    "finding_count": len(decision.findings),
                    "report": str(attempt_report),
                },
            )
            self.emit(
                "integrity_review_finished",
                "succeeded",
                scope=review_scope,
                data={"gate": "approved" if decision.verdict == "approve" else "quarantined",
                      "profile": profile, "report": str(normalized_root / "decision.json")},
            )
            return decision, normalized_root / "decision.json"

        report = normalized_root / "decision.json"
        message = "Integrity reviewer was unavailable after all configured attempts: " + "; ".join(failures)
        self.failures[cache_key] = message
        write_json(report, {"schema": "integrity_execution/1", "status": "failed", "error": message})
        report.chmod(0o600)
        self.emit(
            "integrity_review_finished",
            "failed",
            scope=review_scope,
            data={"gate": "unavailable", "profile": profile, "report": str(report)},
        )
        raise RuntimeError(message)
