"""Private owner dashboard rendered from the raw event ledger and run evidence.

Everything in this module is controller-side output for the run owner. Pages
are written to ``run_dir/status.html`` and ``run_dir/owner/`` only — never into
``run_dir/public`` and never into any worker-visible directory. The public
sanitized pages remain the responsibility of :mod:`.viewers`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ._owner_evidence import (
    DIFF_PREVIEW_LINES,
    compact_text,
    parse_transcript,
    preview_stream,
    read_json,
    resolve_recorded_path,
    resolve_role_output,
    unified_diff,
    write_owner_manifest,
)
from .events import read_events, rebuild_run_state
from .viewers import (
    _STYLE,
    _duration_text,
    _format_ts,
    _h,
    _label,
    _mapping,
    _atomic_write,
    _parse_ts,
    _render_coverage,
    _render_iterations,
    _render_matrix,
    _render_phase_graph,
    _safe_status,
    _sequence,
    _status_icon,
    _status_line,
)


_TRACE_FILES = (
    "raw.stdout.stream",
    "raw.stderr.stream",
    "chunks.jsonl",
    "invocation.json",
    "outcome.json",
    "workspace.json",
)

_BANNER = (
    '<div class="private-banner"><strong>PRIVATE</strong><span>Owner evidence view: '
    "validation and hidden results, reviewer findings, provider details, and raw "
    "traces. Never publish this page or the run directory; share only the "
    "<code>public/</code> bundle.</span></div>"
)

_OWNER_STYLE = """
    :root { --warn-bg: #fff1f0; --warn-border: #ffb3ad; --warn-text: #a40e26; }
    @media (prefers-color-scheme: dark) { :root {
      --warn-bg: #3b1219; --warn-border: #8e1519; --warn-text: #ffb3ad; } }
    .private-banner { display: flex; align-items: baseline; gap: 10px; padding: 10px 24px;
      background: var(--warn-bg); border-bottom: 1px solid var(--warn-border);
      color: var(--warn-text); font-size: 13px; }
    .private-banner strong { letter-spacing: 1px; }
    .owner-tag { display: inline-block; margin-left: 6px; padding: 1px 7px;
      border: 1px solid var(--warn-border); border-radius: 10px;
      color: var(--warn-text); font-size: 11px; font-weight: 600; }
    .attempt-link { display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
      min-height: 44px; padding: 10px 14px; border-top: 1px solid var(--line);
      color: var(--text); }
    .attempt-link:hover { background: var(--hover); text-decoration: none; }
    .attempt-link code { min-width: 168px; }
    .attempt-link .grow { flex: 1 1 120px; min-width: 0; color: var(--muted); }
    .attempt-link .score { font-weight: 600; }
    .attempt-link .dur { color: var(--muted); font-size: 12px; }
    .evidence-list { margin: 6px 0; padding-left: 20px; }
    .evidence-list li { margin: 3px 0; }
    .missing { color: var(--muted); font-style: italic; }
    .kv { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px;
      margin: 8px 0 14px; }
    .kv div { min-width: 0; } .kv span { display: block; color: var(--muted); font-size: 11px; }
    .kv strong { overflow-wrap: anywhere; font-weight: 600; }
    pre.preview { max-width: 100%; max-height: 420px; margin: 6px 0; padding: 10px 12px;
      overflow: auto; border: 1px solid var(--line); border-radius: 6px;
      background: var(--chip);
      font: 12px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .tr-scroll { max-height: 420px; overflow-y: auto; margin: 6px 0; padding: 4px 12px;
      border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }
    .truncated { color: var(--amber); font-size: 12px; }
    .tr-entry { border-top: 1px solid var(--line); padding: 6px 0; }
    .tr-entry:first-child { border-top: 0; }
    .tr-entry > span { color: var(--muted); font-size: 11px; font-weight: 600; }
    .tr-entry pre { margin: 3px 0 0; white-space: pre-wrap; overflow-wrap: anywhere;
      font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .owner-section { margin-top: 26px; }
    .step-detail { border: 1px solid var(--line); border-radius: 8px;
      background: var(--panel); }
    .step-detail + .step-detail { margin-top: 10px; }
    .step-detail > summary { display: flex; align-items: center; gap: 10px;
      padding: 10px 14px; cursor: pointer; font-weight: 600; }
    .step-detail .dur { margin-left: auto; color: var(--muted); font-size: 12px;
      font-weight: 400; }
    .step-detail .body { padding: 0 14px 12px; }
    @media (max-width: 820px) {
      .kv { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .attempt-link code { min-width: 0; flex: 1 1 140px; }
    }
"""


def render_owner_views(events_path: Path, run_dir: Path, *, final: bool = False) -> None:
    """Render the owner index, per-attempt and per-role pages, atomically."""

    events = read_events(events_path)
    state = rebuild_run_state(events_path)
    details = _collect_private_details(events)
    now = _parse_ts(state.get("updated_at"))

    owner_dir = run_dir / "owner"
    (owner_dir / "attempts").mkdir(parents=True, exist_ok=True)
    (owner_dir / "roles").mkdir(parents=True, exist_ok=True)

    for value in _sequence(state.get("attempts")):
        attempt = _mapping(value)
        attempt_id = str(attempt.get("attempt_id"))
        page = owner_dir / "attempts" / f"{attempt_id}.html"
        terminal = _safe_status(attempt.get("status")) not in {"pending", "running"}
        if terminal and page.is_file() and not final:
            continue
        _atomic_write(
            page,
            _render_attempt_page(attempt, details, state, run_dir, now),
        )

    for value in _sequence(state.get("roles")):
        role = _mapping(value)
        job_id = str(role.get("job_id"))
        page = owner_dir / "roles" / f"{job_id}.html"
        terminal = _safe_status(role.get("status")) not in {"pending", "running"}
        if terminal and page.is_file() and not final:
            continue
        _atomic_write(page, _render_role_page(role, details, run_dir, now))

    _atomic_write(run_dir / "status.html", _render_owner_index(state, details, now))
    if final:
        write_owner_manifest(state, run_dir)


def _collect_private_details(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Gather controller-private per-attempt and per-role fields from raw events."""

    attempts: dict[str, dict[str, Any]] = {}
    roles: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = str(event.get("kind", ""))
        scope = _mapping(event.get("scope"))
        data = _mapping(event.get("data"))
        attempt_id = scope.get("attempt_id")
        if attempt_id is not None:
            record = attempts.setdefault(
                str(attempt_id),
                {"steps": {}, "review_attempts": [], "worker": {}},
            )
            if kind == "attempt_started":
                for key in ("parent_id", "guidance"):
                    if key in data:
                        record[key] = data.get(key)
                if isinstance(data.get("worker"), str):
                    record["worker_name"] = data["worker"]
            elif kind == "attempt_finished":
                record.update(
                    {key: value for key, value in data.items() if key != "steps"}
                )
            elif kind == "worker_finished":
                record["worker"] = dict(data)
            elif kind == "step_finished":
                name = str(scope.get("step", "step"))
                record["steps"][name] = {
                    "metrics": _mapping(data.get("metrics")),
                    "artifacts": _sequence(data.get("artifacts")),
                    "error": data.get("error"),
                    "verdict": data.get("verdict"),
                }
            elif kind == "integrity_review_attempt_finished":
                record["review_attempts"].append(
                    {
                        "review_attempt": data.get("review_attempt"),
                        "status": event.get("status"),
                        "verdict": data.get("verdict"),
                        "finding_count": data.get("finding_count"),
                        "report": data.get("report"),
                        "error": data.get("error"),
                    }
                )
            elif kind == "integrity_review_finished":
                record["gate"] = data.get("gate")
        job_id = scope.get("job_id")
        if job_id is not None and kind in {"role_agent_started", "role_agent_finished"}:
            role = roles.setdefault(str(job_id), {})
            role.setdefault("role", str(scope.get("role", "")))
            if kind == "role_agent_finished":
                role.update(dict(data))
    return {"attempts": attempts, "roles": roles}


def _render_owner_index(
    state: Mapping[str, Any], details: Mapping[str, Any], now: datetime | None
) -> str:
    run = _mapping(state.get("run"))
    run_status = _safe_status(run.get("status"))
    refresh = (
        '<meta http-equiv="refresh" content="5">' if run_status in {"pending", "running"} else ""
    )
    head = _owner_run_head(run, run_status, state, now)
    phases = _render_phase_graph(state.get("phases"), now)
    iterations = _render_iterations(state.get("iterations"), now)
    attempts_by_id = {
        str(_mapping(value).get("attempt_id")): _mapping(value)
        for value in _sequence(state.get("attempts"))
    }
    matrix = _render_matrix(state.get("matrix"), attempts_by_id, now)
    attempts = _render_owner_attempt_groups(state.get("attempts"), details, now)
    roles = _render_owner_roles(state.get("roles"), now)
    coverage = _render_coverage(state.get("trace_coverage"))
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptiProfiler Evolve Owner Console</title>
  <style>{_STYLE}{_OWNER_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Owner console</span></header>
  {_BANNER}
  <div class="layout">
    <aside><h2>Run details</h2><nav><a href="#summary">Summary</a><a href="#workflow">Workflow</a><a href="#matrix">Island matrix</a><a href="#attempts">Attempts</a><a href="#roles">Agent jobs</a><a href="#coverage">Trace coverage</a></nav></aside>
    <main>
      <section id="summary">{head}</section>
      <section id="workflow"><h2>Workflow</h2>{phases}{iterations}</section>
      <section id="matrix"><h2>Island matrix</h2>{matrix}</section>
      <section id="attempts"><h2>Attempts</h2>{attempts}</section>
      <section id="roles"><h2>Trusted agent jobs</h2>{roles}</section>
      <section id="coverage"><h2>Agent trace coverage</h2>{coverage}</section>
      <p class="footnote">Private owner console generated from the raw event ledger. Every attempt and trusted agent job links to a detail page with full evidence.</p>
    </main>
  </div>
</body>
</html>
"""
    return document


def _owner_run_head(
    run: Mapping[str, Any],
    run_status: str,
    state: Mapping[str, Any],
    now: datetime | None,
) -> str:
    parts = [_label(run_status)]
    started = _parse_ts(_mapping(run.get("started")).get("ts"))
    if started is not None:
        parts.append(f"Started {_format_ts(started)}")
    duration = _duration_text(run, now)
    if duration:
        parts.append(f"Duration {duration}")
    parts.append(f"Event {state.get('last_seq', 0)}")
    subhead = " · ".join(_h(part) for part in parts)
    best = run.get("best_candidate_id") or "Not selected"
    return (
        f'<div class="run-head">{_status_icon(run_status)}'
        f'<div><h1>Evolution run<span class="owner-tag">Private</span></h1>'
        f'<p class="subhead">{subhead}</p></div></div>'
        '<div class="run-summary">'
        f'<div class="metric"><span>Best candidate</span><strong><code>{_h(best)}'
        "</code></strong></div>"
        f'<div class="metric"><span>Iterations</span>'
        f"<strong>{len(_sequence(state.get('iterations')))}</strong></div>"
        f'<div class="metric"><span>Attempts</span>'
        f"<strong>{len(_sequence(state.get('attempts')))}</strong></div>"
        "</div>"
    )


def _render_owner_attempt_groups(
    values: object, details: Mapping[str, Any], now: datetime | None
) -> str:
    attempts = _sequence(values)
    if not attempts:
        return '<p class="empty">No candidate attempts recorded yet.</p>'
    groups: dict[object, list[Mapping[str, Any]]] = {}
    order: list[object] = []
    for value in attempts:
        attempt = _mapping(value)
        iteration = attempt.get("iteration")
        if iteration not in groups:
            groups[iteration] = []
            order.append(iteration)
        groups[iteration].append(attempt)
    latest = max(
        (iteration for iteration in groups if isinstance(iteration, int)),
        default=None,
    )
    private_attempts = _mapping(details.get("attempts"))
    sections = []
    for iteration in order:
        members = groups[iteration]
        active = any(
            _safe_status(member.get("status")) in {"running", "failed"} for member in members
        )
        open_attr = " open" if iteration == latest or active else ""
        title = f"Iteration {iteration}" if iteration is not None else "Other attempts"
        rows = []
        for member in members:
            attempt_id = str(member.get("attempt_id"))
            status = _safe_status(member.get("status"))
            score = member.get("public_score")
            score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "-"
            private = _mapping(private_attempts.get(attempt_id))
            validation = private.get("validation_score")
            validation_text = (
                f"{validation:.4f}"
                if isinstance(validation, (int, float)) and private.get("valid")
                else "-"
            )
            island = member.get("island")
            rows.append(
                f'<a class="attempt-link" href="{_href("owner/attempts", attempt_id)}">'
                f"{_status_icon(status)}<code>{_h(attempt_id)}</code>"
                f'<span class="grow">Island {_h(island if island is not None else "-")}</span>'
                f'<span class="score">Public {_h(score_text)}</span>'
                f'<span class="score">Val {_h(validation_text)}</span>'
                f'<span class="dur">{_h(_duration_text(member, now))}</span></a>'
            )
        sections.append(
            f'<details class="iter-group"{open_attr}><summary>{_h(title)}'
            f'<span class="count">{len(members)} attempts</span></summary>{"".join(rows)}'
            "</details>"
        )
    return "".join(sections)


def _render_owner_roles(values: object, now: datetime | None) -> str:
    roles = _sequence(values)
    if not roles:
        return '<p class="empty">No trusted agent jobs recorded.</p>'
    rows = []
    for value in roles:
        role = _mapping(value)
        job_id = str(role.get("job_id"))
        rows.append(
            "<tr>"
            f'<td><a href="{_href("owner/roles", job_id)}"><code>{_h(job_id)}</code></a></td>'
            f"<td>{_h(role.get('role'))}</td>"
            f"<td>{_h(role.get('phase') or '-')}</td>"
            f"<td>{_status_line(_safe_status(role.get('status')))}</td>"
            f"<td>{_h(_duration_text(role, now) or '-')}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Job</th><th>Role</th><th>Phase</th><th>Status</th><th>Duration</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _render_attempt_page(
    attempt: Mapping[str, Any],
    details: Mapping[str, Any],
    state: Mapping[str, Any],
    run_dir: Path,
    now: datetime | None,
) -> str:
    attempt_id = str(attempt.get("attempt_id"))
    status = _safe_status(attempt.get("status"))
    refresh = (
        '<meta http-equiv="refresh" content="5">' if status in {"pending", "running"} else ""
    )
    private = _mapping(_mapping(details.get("attempts")).get(attempt_id))
    page_dir = run_dir / "owner" / "attempts"

    header = _attempt_header(attempt, private, state, now)
    scores = _attempt_scores(attempt, private, run_dir, page_dir, attempt_id)
    worker = _attempt_worker_section(private, run_dir, page_dir, attempt_id)
    steps = _attempt_steps_section(attempt, private, run_dir, page_dir, now)
    diff = _attempt_diff_section(private, run_dir, page_dir, attempt_id)
    review = _attempt_review_section(private, run_dir, page_dir, attempt_id)
    evaluation = _attempt_evaluation_section(private, run_dir, page_dir, attempt_id)
    gateway = _attempt_gateway_section(private, run_dir, page_dir)

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Attempt {_h(attempt_id)} — Owner Console</title>
  <style>{_STYLE}{_OWNER_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Owner console</span></header>
  {_BANNER}
  <main class="report">
    <p><a href="../../status.html">&larr; Owner console</a></p>
    {header}
    {scores}
    <section class="owner-section"><h2>Worker invocation</h2>{worker}</section>
    <section class="owner-section"><h2>Steps</h2>{steps}</section>
    <section class="owner-section"><h2>Source changes</h2>{diff}</section>
    <section class="owner-section"><h2>Integrity review</h2>{review}</section>
    <section class="owner-section"><h2>Benchmark evidence</h2>{evaluation}</section>
    <section class="owner-section"><h2>Provider gateway</h2>{gateway}</section>
    <p class="footnote">Private owner evidence page. Share only the public/ bundle.</p>
  </main>
</body>
</html>
"""
    return document


def _attempt_header(
    attempt: Mapping[str, Any],
    private: Mapping[str, Any],
    state: Mapping[str, Any],
    now: datetime | None,
) -> str:
    attempt_id = str(attempt.get("attempt_id"))
    status = _safe_status(attempt.get("status"))
    parts = [_label(status)]
    duration = _duration_text(attempt, now)
    if duration:
        parts.append(f"Duration {duration}")
    island = attempt.get("island")
    iteration = attempt.get("iteration")
    parts.append(f"Iteration {iteration if iteration is not None else '-'}")
    parts.append(f"Island {island if island is not None else '-'}")
    subhead = " · ".join(_h(str(part)) for part in parts)
    parent_id = private.get("parent_id") or attempt.get("parent_id") or "-"
    known = {
        str(_mapping(item).get("attempt_id")) for item in _sequence(state.get("attempts"))
    }
    if isinstance(parent_id, str) and parent_id in known:
        parent_html = f'<a href="{_href(".", parent_id)}"><code>{_h(parent_id)}</code></a>'
    else:
        parent_html = f"<code>{_h(parent_id)}</code>"
    worker_name = private.get("worker_name")
    accepted = private.get("accepted")
    accepted_text = "Yes" if accepted is True else "No" if accepted is False else "-"
    return (
        f'<div class="run-head">{_status_icon(status)}'
        f'<div><h1><code>{_h(attempt_id)}</code>'
        '<span class="owner-tag">Private</span></h1>'
        f'<p class="subhead">{subhead}</p></div></div>'
        '<div class="kv">'
        f"<div><span>Parent</span><strong>{parent_html}</strong></div>"
        f"<div><span>Worker</span><strong><code>{_h(worker_name or 'unavailable')}"
        "</code></strong></div>"
        f"<div><span>Accepted</span><strong>{_h(accepted_text)}</strong></div>"
        f"<div><span>Guidance</span><strong>{_h(private.get('guidance') or '-')}"
        "</strong></div>"
        "</div>"
        + (
            f'<p class="subhead">Error: <code>{_h(private.get("error"))}</code></p>'
            if private.get("error")
            else ""
        )
    )


def _attempt_scores(
    attempt: Mapping[str, Any],
    private: Mapping[str, Any],
    run_dir: Path,
    page_dir: Path,
    attempt_id: str,
) -> str:
    public = attempt.get("public_score", private.get("public_score"))
    public_text = f"{public:.6f}" if isinstance(public, (int, float)) else "unavailable"
    validation_dir = run_dir / "controller" / "evaluations" / attempt_id / "validation"
    validation = private.get("validation_score")
    if validation_dir.is_dir() and isinstance(validation, (int, float)):
        validation_html = (
            f"<strong>{_h(f'{validation:.6f}')}</strong> "
            f"{_link_or_missing(run_dir, validation_dir, page_dir, 'evaluation output')}"
        )
    else:
        validation_html = '<span class="missing">unavailable (not evaluated)</span>'
    hidden_dir = run_dir / "controller" / "final_evaluations" / attempt_id
    if hidden_dir.is_dir():
        hidden_html = _link_or_missing(run_dir, hidden_dir, page_dir, "final evaluation output")
    else:
        hidden_html = '<span class="missing">unavailable (finalists only)</span>'
    return (
        '<div class="kv">'
        f"<div><span>Public score</span><strong>{_h(public_text)}</strong></div>"
        f'<div><span>Validation score <span class="owner-tag">never worker-visible</span>'
        f"</span>{validation_html}</div>"
        f'<div><span>Hidden result <span class="owner-tag">never worker-visible</span>'
        f"</span>{hidden_html}</div>"
        "</div>"
    )


def _attempt_worker_section(
    private: Mapping[str, Any], run_dir: Path, page_dir: Path, attempt_id: str
) -> str:
    worker = _mapping(private.get("worker"))
    transcript = run_dir / "transcripts" / f"{attempt_id}.jsonl"
    trace_dir = run_dir / "traces" / attempt_id
    facts = [
        ("Return code", worker.get("returncode", private.get("worker_returncode", "-"))),
        ("Timed out", worker.get("timed_out", private.get("worker_timed_out", "-"))),
        ("Cancelled", worker.get("cancelled", private.get("worker_cancelled", "-"))),
        (
            "Termination",
            worker.get("termination_reason", private.get("worker_termination_reason"))
            or "-",
        ),
    ]
    kv = "".join(
        f"<div><span>{_h(name)}</span><strong>{_h(value)}</strong></div>"
        for name, value in facts
    )
    capture_error = worker.get("trace_capture_error") or private.get("trace_capture_error")
    capture = (
        f'<p class="truncated">Trace capture degraded: <code>{_h(capture_error)}</code></p>'
        if capture_error
        else ""
    )
    transcript_html = _transcript_preview(transcript, run_dir, page_dir)
    streams_html = _stream_previews(trace_dir, run_dir, page_dir)
    links = [
        _link_or_missing(run_dir, trace_dir / name, page_dir, name) for name in _TRACE_FILES
    ]
    links.append(_link_or_missing(run_dir, trace_dir, page_dir, "trace directory"))
    return (
        f'<div class="kv">{kv}</div>{capture}'
        "<h3>Transcript (messages and tool calls)</h3>"
        f"{transcript_html}"
        f"{streams_html}"
        "<h3>Raw capture</h3>"
        f'<ul class="evidence-list">{"".join(f"<li>{item}</li>" for item in links)}</ul>'
    )


def _stream_previews(trace_dir: Path, run_dir: Path, page_dir: Path) -> str:
    sections = []
    for name, title in (
        ("raw.stdout.stream", "Captured stdout"),
        ("raw.stderr.stream", "Captured stderr"),
    ):
        preview = preview_stream(trace_dir / name)
        if preview is None:
            sections.append(
                f'<h3>{_h(title)}</h3><p class="missing">{_h(name)} (unavailable)</p>'
            )
            continue
        text, truncated = preview
        note = (
            '<p class="truncated">Preview truncated; open the full stream below.</p>'
            if truncated
            else ""
        )
        link = _relative_link(trace_dir / name, page_dir, run_dir)
        sections.append(
            f'<h3>{_h(title)}</h3><pre class="preview">{_h(text)}</pre>{note}'
            f"<p>Full stream: {link}</p>"
        )
    return "".join(sections)


def _attempt_steps_section(
    attempt: Mapping[str, Any],
    private: Mapping[str, Any],
    run_dir: Path,
    page_dir: Path,
    now: datetime | None,
) -> str:
    steps = _sequence(attempt.get("steps"))
    if not steps:
        return '<p class="empty">No steps recorded yet.</p>'
    private_steps = _mapping(private.get("steps"))
    rendered = []
    for value in steps:
        step = _mapping(value)
        name = str(step.get("name", "step"))
        status = _safe_status(step.get("status"))
        data = _mapping(private_steps.get(name))
        verdict = data.get("verdict") or step.get("verdict")
        duration = _duration_text(step, now)
        metrics = _mapping(data.get("metrics"))
        metric_rows = "".join(
            f"<tr><td>{_h(key)}</td><td><code>{_h(compact_text(value))}</code></td></tr>"
            for key, value in sorted(metrics.items())
        )
        metrics_html = (
            f'<div class="table-wrap"><table><tbody>{metric_rows}</tbody></table></div>'
            if metric_rows
            else '<p class="missing">No metrics recorded.</p>'
        )
        artifact_items = []
        for artifact in _sequence(data.get("artifacts")):
            resolved = resolve_recorded_path(str(artifact), run_dir)
            if resolved is not None:
                artifact_items.append(_relative_link(resolved, page_dir, run_dir))
            else:
                artifact_items.append(
                    f'<span class="missing">{_h(artifact)} (unavailable)</span>'
                )
        artifacts_html = (
            '<ul class="evidence-list">'
            + "".join(f"<li>{item}</li>" for item in artifact_items)
            + "</ul>"
            if artifact_items
            else ""
        )
        error = data.get("error")
        error_html = (
            f'<p>Error: <code>{_h(error)}</code></p>' if error else ""
        )
        open_attr = " open" if status == "failed" else ""
        summary_bits = _label(verdict or status)
        rendered.append(
            f'<details class="step-detail"{open_attr}><summary>{_status_icon(status)}'
            f"{_h(name)}<span>{_h(summary_bits)}</span>"
            f'<span class="dur">{_h(duration)}</span></summary>'
            f'<div class="body">{error_html}{metrics_html}{artifacts_html}</div></details>'
        )
    return "".join(rendered)


def _attempt_diff_section(
    private: Mapping[str, Any], run_dir: Path, page_dir: Path, attempt_id: str
) -> str:
    changed = [str(item) for item in _sequence(private.get("changed_files"))]
    changed_html = (
        '<ul class="evidence-list">'
        + "".join(f"<li><code>{_h(name)}</code></li>" for name in changed)
        + "</ul>"
        if changed
        else '<p class="missing">No changed files recorded.</p>'
    )
    candidate_root = run_dir / "workspaces" / attempt_id
    if not candidate_root.is_dir():
        candidate_root = run_dir / "candidates" / attempt_id
    parent_id = private.get("parent_id")
    parent_root = None
    if isinstance(parent_id, str) and parent_id:
        for base in ("candidates", "workspaces"):
            candidate = run_dir / base / parent_id
            if candidate.is_dir():
                parent_root = candidate
                break
    diff_html = '<p class="missing">Diff unavailable (missing candidate or parent tree).</p>'
    if candidate_root.is_dir() and parent_root is not None:
        patch, truncated = unified_diff(parent_root, candidate_root)
        if not patch:
            diff_html = '<p class="missing">No textual differences from parent.</p>'
        else:
            patch_path = page_dir / f"{attempt_id}.diff.patch"
            _atomic_write(patch_path, patch)
            lines = patch.splitlines()
            preview = "\n".join(lines[:DIFF_PREVIEW_LINES])
            note = (
                '<p class="truncated">Diff truncated at generation limits.</p>'
                if truncated
                else ""
            )
            more = (
                f'<p class="truncated">Preview shows first {DIFF_PREVIEW_LINES} of '
                f"{len(lines)} lines.</p>"
                if len(lines) > DIFF_PREVIEW_LINES
                else ""
            )
            link = _relative_link(patch_path, page_dir, run_dir)
            diff_html = (
                f'<pre class="preview">{_h(preview)}</pre>{more}{note}'
                f'<p>Full patch: {link}</p>'
            )
    workspace_link = (
        _link_or_missing(run_dir, candidate_root, page_dir, "candidate tree")
        if candidate_root.is_dir()
        else '<span class="missing">candidate tree (unavailable)</span>'
    )
    return f"<h3>Changed files</h3>{changed_html}<h3>Diff vs parent</h3>{diff_html}<p>{workspace_link}</p>"


def _attempt_review_section(
    private: Mapping[str, Any], run_dir: Path, page_dir: Path, attempt_id: str
) -> str:
    gate = private.get("gate") or "pending"
    review_root = run_dir / "controller" / "integrity_reviews" / attempt_id
    decision_path = review_root / "decision.json"
    decision_html = '<p class="missing">Decision file unavailable.</p>'
    if decision_path.is_file():
        decision = read_json(decision_path)
        findings = _sequence(_mapping(decision).get("findings"))
        finding_items = []
        for value in findings:
            finding = _mapping(value)
            finding_items.append(
                "<li>"
                f"<strong>{_h(finding.get('severity', 'finding'))}</strong>: "
                f"{_h(finding.get('summary', ''))} "
                f"<code>{_h(finding.get('evidence', ''))}</code>"
                "</li>"
            )
        findings_html = (
            f'<ul class="evidence-list">{"".join(finding_items)}</ul>'
            if finding_items
            else "<p>No findings.</p>"
        )
        decision_html = (
            '<div class="kv">'
            f"<div><span>Verdict</span><strong>{_h(_mapping(decision).get('verdict', '-'))}"
            "</strong></div>"
            f"<div><span>Reason</span><strong>{_h(_mapping(decision).get('reason') or '-')}"
            "</strong></div>"
            "</div>"
            f"<p>{_h(_mapping(decision).get('summary', ''))}</p>"
            f"<h3>Findings</h3>{findings_html}"
            f"<p>{_relative_link(decision_path, page_dir, run_dir)}</p>"
        )
    invocations = _sequence(private.get("review_attempts"))
    rows = []
    for value in invocations:
        invocation = _mapping(value)
        number = invocation.get("review_attempt")
        job_id = f"{attempt_id}-r{int(number):02d}" if isinstance(number, int) else None
        job_link = (
            f'<a href="{_href("../roles", job_id)}"><code>{_h(job_id)}</code></a>'
            if job_id is not None
            else "-"
        )
        report = resolve_recorded_path(str(invocation.get("report") or ""), run_dir)
        report_html = (
            _relative_link(report, page_dir, run_dir)
            if report is not None
            else '<span class="missing">report unavailable</span>'
        )
        outcome = invocation.get("verdict") or invocation.get("error") or "-"
        rows.append(
            "<tr>"
            f"<td>{_h(number if number is not None else '-')}</td>"
            f"<td>{_status_line(_safe_status(invocation.get('status')))}</td>"
            f"<td><code>{_h(compact_text(outcome))}</code></td>"
            f"<td>{job_link}</td>"
            f"<td>{report_html}</td>"
            "</tr>"
        )
    invocations_html = (
        '<div class="table-wrap"><table><thead><tr><th>#</th><th>Status</th><th>Outcome</th><th>Reviewer invocation</th><th>Report</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
        if rows
        else '<p class="missing">No reviewer invocations recorded.</p>'
    )
    return (
        f'<div class="kv"><div><span>Gate</span><strong>{_h(_label(gate))}</strong></div></div>'
        f"{decision_html}<h3>Reviewer invocations</h3>{invocations_html}"
    )


def _attempt_evaluation_section(
    private: Mapping[str, Any], run_dir: Path, page_dir: Path, attempt_id: str
) -> str:
    broker_artifacts = run_dir / "controller" / "brokers" / attempt_id / "artifacts"
    items = [
        _link_or_missing(
            run_dir, broker_artifacts, page_dir, "worker-visible benchmark artifacts"
        )
    ]
    if broker_artifacts.is_dir():
        for name in ("feedback.md", "artifact_index.json"):
            for found in sorted(broker_artifacts.rglob(name))[:4]:
                items.append(_relative_link(found, page_dir, run_dir))
    return '<ul class="evidence-list">' + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _attempt_gateway_section(
    private: Mapping[str, Any], run_dir: Path, page_dir: Path
) -> str:
    worker = _mapping(private.get("worker"))
    outcome = worker.get("provider_gateway_outcome")
    count = worker.get("provider_gateway_request_count")
    manifest = resolve_recorded_path(
        str(worker.get("provider_gateway_manifest") or ""), run_dir
    )
    manifest_html = (
        _relative_link(manifest, page_dir, run_dir)
        if manifest is not None
        else '<span class="missing">gateway manifest unavailable</span>'
    )
    return (
        '<div class="kv">'
        f"<div><span>Outcome</span><strong>{_h(outcome or 'unavailable')}</strong></div>"
        f"<div><span>Requests</span><strong>{_h(count if count is not None else '-')}"
        "</strong></div>"
        f"<div><span>Audit manifest</span>{manifest_html}</div>"
        "</div>"
    )


def _render_role_page(
    role: Mapping[str, Any],
    details: Mapping[str, Any],
    run_dir: Path,
    now: datetime | None,
) -> str:
    job_id = str(role.get("job_id"))
    role_name = str(role.get("role", ""))
    status = _safe_status(role.get("status"))
    refresh = (
        '<meta http-equiv="refresh" content="5">' if status in {"pending", "running"} else ""
    )
    private = _mapping(_mapping(details.get("roles")).get(job_id))
    page_dir = run_dir / "owner" / "roles"
    transcript = run_dir / "research" / "transcripts" / role_name / f"{job_id}.jsonl"
    trace_dir = run_dir / "research" / "traces" / role_name / job_id
    parts = [_label(status)]
    duration = _duration_text(role, now)
    if duration:
        parts.append(f"Duration {duration}")
    if role.get("phase"):
        parts.append(f"Phase {role.get('phase')}")
    subhead = " · ".join(_h(str(part)) for part in parts)
    facts = [
        ("Role", role_name or "-"),
        ("Return code", private.get("returncode", "-")),
        ("Timed out", private.get("timed_out", "-")),
        ("Termination", private.get("termination_reason") or "-"),
    ]
    kv = "".join(
        f"<div><span>{_h(name)}</span><strong>{_h(value)}</strong></div>"
        for name, value in facts
    )
    workspace = run_dir / "research" / "roles" / role_name / job_id
    outputs = _sequence(private.get("outputs"))
    output_items = []
    for name in outputs:
        target = resolve_role_output(workspace, str(name))
        if target is not None:
            output_items.append(_relative_link(target, page_dir, run_dir))
        else:
            output_items.append(f'<span class="missing">{_h(name)} (unavailable)</span>')
    outputs_html = (
        '<ul class="evidence-list">'
        + "".join(f"<li>{item}</li>" for item in output_items)
        + "</ul>"
        if output_items
        else '<p class="missing">No declared outputs.</p>'
    )
    links = [
        _link_or_missing(run_dir, trace_dir / name, page_dir, name) for name in _TRACE_FILES
    ]
    links.append(_link_or_missing(run_dir, trace_dir, page_dir, "trace directory"))
    links.append(_link_or_missing(run_dir, workspace, page_dir, "role workspace"))
    transcript_html = _transcript_preview(transcript, run_dir, page_dir)
    streams_html = _stream_previews(trace_dir, run_dir, page_dir)
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent job {_h(job_id)} — Owner Console</title>
  <style>{_STYLE}{_OWNER_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Owner console</span></header>
  {_BANNER}
  <main class="report">
    <p><a href="../../status.html">&larr; Owner console</a></p>
    <div class="run-head">{_status_icon(status)}<div><h1><code>{_h(job_id)}</code><span class="owner-tag">Private</span></h1><p class="subhead">{subhead}</p></div></div>
    <div class="kv">{kv}</div>
    <section class="owner-section"><h2>Transcript (messages and tool calls)</h2>{transcript_html}</section>
    <section class="owner-section"><h2>Raw capture</h2>{streams_html}<ul class="evidence-list">{"".join(f"<li>{item}</li>" for item in links)}</ul></section>
    <section class="owner-section"><h2>Declared outputs</h2>{outputs_html}</section>
    <p class="footnote">Private owner evidence page. Share only the public/ bundle.</p>
  </main>
</body>
</html>
"""
    return document


def _transcript_preview(transcript: Path, run_dir: Path, page_dir: Path) -> str:
    if not transcript.is_file():
        return '<p class="missing">Transcript unavailable.</p>'
    entries, fallback, truncated = parse_transcript(transcript)
    if entries:
        body = "".join(
            f'<div class="tr-entry"><span>{_h(label)}</span><pre>{_h(text)}</pre></div>'
            for label, text in entries
        )
        preview = f'<div class="tr-scroll">{body}</div>'
    else:
        preview = f'<pre class="preview">{_h(fallback)}</pre>'
    note = (
        '<p class="truncated">Preview truncated; open the full transcript below.</p>'
        if truncated
        else ""
    )
    link = _relative_link(transcript, page_dir, run_dir)
    return f"{preview}{note}<p>Full transcript: {link}</p>"


def _relative_link(target: Path, page_dir: Path, run_dir: Path) -> str:
    relative = os.path.relpath(target, page_dir)
    href = quote(relative.replace(os.sep, "/"))
    label = os.path.relpath(target, run_dir).replace(os.sep, "/")
    return f'<a href="{_h(href)}"><code>{_h(label)}</code></a>'


def _link_or_missing(run_dir: Path, target: Path, page_dir: Path, label: str) -> str:
    if target.exists() and not target.is_symlink():
        return _relative_link(target, page_dir, run_dir)
    return f'<span class="missing">{_h(label)} (unavailable)</span>'


def _href(prefix: str, name: str) -> str:
    return _h(f"{prefix}/{quote(str(name))}.html")


__all__: list[str] = []
