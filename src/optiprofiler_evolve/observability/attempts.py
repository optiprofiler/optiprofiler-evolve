"""Project completed attempts into ledger evidence."""

from __future__ import annotations

from optiprofiler_evolve.agents.explanations import explanation_memory, explanation_status
from optiprofiler_evolve.agents.jobs import worker_exit_warning as _worker_exit_warning
from optiprofiler_evolve.models import (
    Attempt,
    CandidateRecord,
)


def attempt_summary(attempt: Attempt, record: CandidateRecord) -> dict[str, object]:
    worker_exit_warning = _worker_exit_warning(attempt.worker_outcome)
    return {
        "attempt_id": record.attempt_id,
        "candidate_id": record.candidate_id,
        "island": record.island,
        "iteration": record.iteration,
        "attempt_index": record.attempt_index,
        "parent_id": record.parent_id,
        "guidance": record.guidance,
        "public_score": record.public_score if attempt.evaluated else None,
        "validation_score": record.validation_score,
        "assessment_score": record.assessment_score,
        "assessment": record.assessment,
        "valid": record.valid,
        "error": record.error,
        "changed_files": list(attempt.changed),
        "worker_returncode": attempt.worker_outcome.returncode,
        "worker_timed_out": attempt.worker_outcome.timed_out,
        "worker_cancelled": attempt.worker_outcome.cancelled,
        "worker_termination_reason": attempt.worker_outcome.termination_reason,
        "worker_exit_warning": worker_exit_warning,
        "transcript": str(attempt.worker_outcome.transcript),
        "native_trace": str(attempt.worker_outcome.native_trace)
        if attempt.worker_outcome.native_trace
        else None,
        "stderr_trace": str(attempt.worker_outcome.stderr_trace)
        if attempt.worker_outcome.stderr_trace
        else None,
        "trace_chunks": str(attempt.worker_outcome.trace_chunks)
        if attempt.worker_outcome.trace_chunks
        else None,
        "trace_outcome": str(attempt.worker_outcome.trace_outcome)
        if attempt.worker_outcome.trace_outcome
        else None,
        "trace_capture_error": attempt.worker_outcome.capture_error,
        "solver_explanation": (
            str(attempt.worker_outcome.solver_explanation)
            if attempt.worker_outcome.solver_explanation
            else None
        ),
        "solver_explanation_status": (
            explanation_status(attempt.worker_outcome.solver_explanation)
            if attempt.worker_outcome.solver_explanation
            else "missing"
        ),
        "solver_explanation_summary": (
            explanation_memory(attempt.worker_outcome.solver_explanation)
            if attempt.worker_outcome.solver_explanation
            else None
        ),
        "usage": attempt.worker_outcome.usage.as_dict(),
    }
