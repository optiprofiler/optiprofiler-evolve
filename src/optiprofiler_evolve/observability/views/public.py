"""Server-free views derived only from the sanitized public event projection."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from optiprofiler_evolve.observability.events import rebuild_run_state, write_run_state
from optiprofiler_evolve.observability.views._html import (
    _STYLE,
    _atomic_write,
    _duration_text,
    _format_ts,
    _h,
    _label,
    _mapping,
    _parse_ts,
    _render_attempt_groups,
    _render_coverage,
    _render_iterations,
    _render_matrix,
    _safe_status,
    _sequence,
    _status_icon,
    _status_line,
)

# Exact (run_dir source name, public/ bundle name) allowlist. The only rename
# maps the sanitized page onto its stable published name: run_dir/status.html
# itself is the PRIVATE owner console and must never enter the bundle.
PUBLIC_BUNDLE_FILES = (
    ("public_events.jsonl", "public_events.jsonl"),
    ("public_run_state.json", "public_run_state.json"),
    ("public_status.html", "status.html"),
    ("report.html", "report.html"),
    ("public_trace_coverage.json", "public_trace_coverage.json"),
    ("PUBLIC_REPORT.md", "PUBLIC_REPORT.md"),
)


def render_status(events_path: Path, destination: Path) -> dict[str, Any]:
    """Atomically render the local Actions-style status view."""

    state = write_run_state(events_path, destination.parent / "public_run_state.json")
    run = state["run"]
    run_status = _safe_status(run.get("status"))
    refresh = (
        '<meta http-equiv="refresh" content="5">' if run_status in {"pending", "running"} else ""
    )
    now = _parse_ts(state.get("updated_at"))
    attempts_by_id = {
        str(_mapping(value).get("attempt_id")): _mapping(value)
        for value in _sequence(state["attempts"])
    }
    head = _render_run_head(run, run_status, state, now, title="Evolution run")
    metrics = _render_metrics(run, state)
    phases = _render_phase_graph(state["phases"], now)
    iterations = _render_iterations(state["iterations"], now)
    matrix = _render_matrix(state["matrix"], attempts_by_id, now)
    attempts = _render_attempt_groups(
        state["attempts"], lambda members: "".join(_render_attempt(member, now) for member in members)
    )
    roles = _render_roles(state["roles"], now)
    coverage = _render_coverage(state.get("trace_coverage"))

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptiProfiler Evolve Status</title>
  <style>{_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Run workflow</span></header>
  <div class="layout">
    <aside><h2>Run details</h2><nav><a href="#summary">Summary</a><a href="#workflow">Workflow</a><a href="#matrix">Island matrix</a><a href="#attempts">Attempts</a><a href="#roles">Research roles</a><a href="#coverage">Trace coverage</a></nav></aside>
    <main>
      <section id="summary">{head}{metrics}</section>
      <section id="workflow"><h2>Workflow</h2>{phases}{iterations}</section>
      <section id="matrix"><h2>Island matrix</h2>{matrix}</section>
      <section id="attempts"><h2>Attempt pipelines</h2>{attempts}</section>
      <section id="roles"><h2>Research roles</h2>{roles}</section>
      <section id="coverage"><h2>Agent trace coverage</h2>{coverage}</section>
      <p class="footnote">This page is generated only from the sanitized public event projection. Private traces, reviewer findings, validation results, and hidden results are not linked or embedded.</p>
    </main>
  </div>
</body>
</html>
"""
    _atomic_write(destination, document)
    return state


def render_public_report(state: Mapping[str, Any], destination: Path) -> None:
    """Write the sanitized Markdown summary consumed by GitHub Job Summary."""

    run = _mapping(state.get("run"))
    lines = [
        "# OptiProfiler Evolve",
        "",
        f"- Status: **{_md(run.get('status', 'pending'))}**",
        f"- Last public event: `{_md(state.get('last_seq', 0))}`",
        f"- Best candidate: `{_md(run.get('best_candidate_id') or 'not selected')}`",
        f"- Attempts: `{len(_sequence(state.get('attempts')))}`",
        "",
        "## Workflow",
        "",
        "| Phase | Status |",
        "|---|---|",
    ]
    phases = _sequence(state.get("phases"))
    if phases:
        for value in phases:
            phase = _mapping(value)
            lines.append(f"| {_md(phase.get('name'))} | {_md(phase.get('status'))} |")
    else:
        lines.append("| No phases recorded | pending |")

    lines.extend(
        [
            "",
            "## Island matrix",
            "",
            "| Iteration | Island | Status | Accepted | Quarantined |",
            "|---:|---:|---|---:|---:|",
        ]
    )
    matrix = _sequence(state.get("matrix"))
    if matrix:
        for value in matrix:
            cell = _mapping(value)
            counts = _mapping(cell.get("counts"))
            lines.append(
                "| "
                f"{_md(cell.get('iteration'))} | {_md(cell.get('island'))} | "
                f"{_md(cell.get('status'))} | {_md(counts.get('accepted', 0))} | "
                f"{_md(counts.get('quarantined', 0))} |"
            )
    else:
        lines.append("| - | - | pending | 0 | 0 |")

    coverage = _mapping(state.get("trace_coverage"))
    if coverage:
        lines.extend(
            [
                "",
                "## Agent trace coverage",
                "",
                f"- Total invocations: `{_md(coverage.get('total', 0))}`",
                f"- Complete captures: `{_md(coverage.get('capture_complete', 0))}`",
                f"- Degraded captures: `{_md(coverage.get('capture_degraded', 0))}`",
                f"- Interrupted captures: `{_md(coverage.get('capture_interrupted', 0))}`",
            ]
        )
    lines.extend(
        [
            "",
            "This summary contains public workflow state only. Validation and hidden results,",
            "reviewer findings, provider details, and raw agent traces remain private.",
        ]
    )
    _atomic_write(destination, "\n".join(lines) + "\n")


def render_final_report(events_path: Path, destination: Path) -> None:
    """Render a stable public final page without private scientific results."""

    state = rebuild_run_state(events_path)
    run = _mapping(state.get("run"))
    run_status = _safe_status(run.get("status"))
    now = _parse_ts(state.get("updated_at"))
    head = _render_run_head(run, run_status, state, now, title="Evolution public report")
    phases = _render_phase_graph(state.get("phases"), now)
    attempts = len(_sequence(state.get("attempts")))
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptiProfiler Evolve Public Report</title>
  <style>{_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Public report</span></header>
  <main class="report">
    {head}
    <div class="run-summary">
      <div class="metric"><span>Best candidate</span><strong><code>{_h(run.get("best_candidate_id") or "not selected")}</code></strong></div>
      <div class="metric"><span>Public attempts recorded</span><strong>{attempts}</strong></div>
      <div class="metric"><span>Last public event</span><strong>{_h(state.get("last_seq", 0))}</strong></div>
    </div>
    <section><h2>Workflow</h2>{phases}</section>
    <section><h2>Public artifacts</h2><ul class="artifact-list"><li><a href="status.html">Actions-style run status</a></li><li><a href="PUBLIC_REPORT.md">Markdown run summary</a></li><li><a href="public_events.jsonl">Public event ledger</a></li><li><a href="public_run_state.json">Public run state</a></li></ul></section>
    <p class="footnote">This report deliberately excludes validation and hidden scores, reviewer findings, provider details, and raw traces.</p>
  </main>
</body>
</html>
"""
    _atomic_write(destination, document)


def materialize_public_bundle(run_dir: Path) -> tuple[Path, ...]:
    """Refresh the exact public artifact allowlist under ``run_dir/public``."""

    destination = run_dir / "public"
    destination.mkdir(parents=True, exist_ok=True)
    available = {
        bundle_name: source_name
        for source_name, bundle_name in PUBLIC_BUNDLE_FILES
        if (run_dir / source_name).is_file() and not (run_dir / source_name).is_symlink()
    }
    for child in tuple(destination.iterdir()):
        if child.name in available and child.is_file() and not child.is_symlink():
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    copied = []
    for source_name, bundle_name in PUBLIC_BUNDLE_FILES:
        if bundle_name not in available:
            continue
        source = run_dir / source_name
        target = destination / bundle_name
        temporary = destination / f".{bundle_name}.tmp"
        with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle)
        temporary.replace(target)
        copied.append(target)
    return tuple(copied)


def _render_run_head(
    run: Mapping[str, Any],
    run_status: str,
    state: Mapping[str, Any],
    now: datetime | None,
    *,
    title: str,
) -> str:
    parts = [_label(run_status)]
    started = _parse_ts(_mapping(run.get("started")).get("ts"))
    if started is not None:
        parts.append(f"Started {_format_ts(started)}")
    duration = _duration_text(run, now)
    if duration:
        parts.append(f"Duration {duration}")
    parts.append(f"Public event {state.get('last_seq', 0)}")
    subhead = " · ".join(_h(part) for part in parts)
    return (
        f'<div class="run-head">{_status_icon(run_status)}'
        f'<div><h1>{_h(title)}</h1><p class="subhead">{subhead}</p></div></div>'
    )


def _render_metrics(run: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    best_candidate = run.get("best_candidate_id") or "Not selected"
    return (
        '<div class="run-summary">'
        f'<div class="metric"><span>Best candidate</span><strong><code>{_h(best_candidate)}'
        "</code></strong></div>"
        f'<div class="metric"><span>Iterations</span>'
        f"<strong>{len(_sequence(state.get('iterations')))}</strong></div>"
        f'<div class="metric"><span>Attempts</span>'
        f"<strong>{len(_sequence(state.get('attempts')))}</strong></div>"
        "</div>"
    )


def _render_phase_graph(values: object, now: datetime | None) -> str:
    phases = _sequence(values)
    if not phases:
        return '<p class="empty">No phases recorded yet.</p>'
    items = []
    for value in phases:
        phase = _mapping(value)
        status = _safe_status(phase.get("status"))
        sub = _label(status)
        duration = _duration_text(phase, now)
        if duration:
            sub += f" · {duration}"
        items.append(
            f'<li class="job-node">{_status_icon(status)}'
            f"<div><strong>{_h(phase.get('name', 'phase'))}</strong>"
            f'<span class="sub">{_h(sub)}</span></div></li>'
        )
    return f'<ol class="job-graph">{"".join(items)}</ol>'


def _render_attempt(attempt: Mapping[str, Any], now: datetime | None) -> str:
    status = _safe_status(attempt.get("status"))
    score = attempt.get("public_score")
    score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "-"
    steps = _sequence(attempt.get("steps"))
    pipeline = "".join(_render_step(_mapping(step), now) for step in steps)
    if not pipeline:
        pipeline = (
            '<div class="step-node"><span class="st pending"></span>'
            "<div><strong>Waiting</strong><span>No steps recorded</span></div></div>"
        )
    worker = _mapping(attempt.get("worker"))
    worker_text = _label(_safe_status(worker.get("status")))
    worker_duration = _duration_text(worker, now)
    if worker_duration:
        worker_text += f" · {worker_duration}"
    review = _mapping(attempt.get("integrity_review"))
    gate = review.get("gate") or review.get("status") or "pending"
    island = attempt.get("island")
    details_open = " open" if status in {"failed", "cancelled"} else ""
    return (
        f'<details class="attempt"{details_open}><summary>{_status_icon(status)}'
        f"<code>{_h(attempt.get('attempt_id'))}</code>"
        f'<span class="grow">Island {_h(island if island is not None else "-")}</span>'
        f'<span class="score">Public {_h(score_text)}</span>'
        f'<span class="dur">{_h(_duration_text(attempt, now))}</span></summary>'
        '<div class="details-body"><div class="meta">'
        f"<div><span>Parent</span><strong><code>{_h(attempt.get('parent_id', '-'))}"
        "</code></strong></div>"
        f"<div><span>Accepted</span><strong>{_h(_accepted_text(attempt.get('accepted')))}"
        "</strong></div>"
        f"<div><span>Worker</span><strong>{_h(worker_text)}</strong></div>"
        f"<div><span>Integrity gate</span><strong>{_h(_label(gate))}</strong></div>"
        f'</div><div class="pipeline">{pipeline}</div></div></details>'
    )


def _render_step(step: Mapping[str, Any], now: datetime | None) -> str:
    status = _safe_status(step.get("status"))
    detail = step.get("verdict") or step.get("outcome") or status
    sub = _label(detail)
    duration = _duration_text(step, now)
    if duration:
        sub += f" · {duration}"
    return (
        f'<div class="step-node">{_status_icon(status)}'
        f"<div><strong>{_h(step.get('name', 'step'))}</strong>"
        f"<span>{_h(sub)}</span></div></div>"
    )


def _render_roles(values: object, now: datetime | None) -> str:
    roles = _sequence(values)
    if not roles:
        return '<p class="empty">No trusted research-role jobs recorded.</p>'
    rows = []
    for value in roles:
        role = _mapping(value)
        rows.append(
            "<tr>"
            f"<td><code>{_h(role.get('job_id'))}</code></td>"
            f"<td>{_h(role.get('role'))}</td>"
            f"<td>{_h(role.get('phase') or '-')}</td>"
            f"<td>{_h(role.get('island') if role.get('island') is not None else '-')}</td>"
            f"<td>{_status_line(_safe_status(role.get('status')))}</td>"
            f"<td>{_h(_duration_text(role, now) or '-')}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Job</th><th>Role</th><th>Phase</th><th>Island</th><th>Status</th><th>Duration</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _accepted_text(value: object) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "-"


def _md(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
