"""Server-free views derived only from the sanitized public event projection."""

from __future__ import annotations

import html
import shutil
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import rebuild_run_state, write_run_state


PUBLIC_BUNDLE_FILES = (
    "public_events.jsonl",
    "public_run_state.json",
    "status.html",
    "report.html",
    "public_trace_coverage.json",
    "PUBLIC_REPORT.md",
)

# Shared workflow-console stylesheet: no scripts, no external assets, and a
# paired light/dark palette selected purely by prefers-color-scheme.
_STYLE = """
    :root { color-scheme: light dark;
      --bg: #f6f8fa; --panel: #ffffff; --line: #d0d7de; --edge: #afb8c1; --muted: #59636e;
      --text: #1f2328; --blue: #0969da; --green: #1a7f37; --red: #cf222e; --amber: #9a6700;
      --gray: #6e7781; --hover: #eaeef2; --chip: #f6f8fa; --th: #f6f8fa;
      --topbar: #24292f; --topbar-muted: #afb8c1; }
    @media (prefers-color-scheme: dark) { :root {
      --bg: #0d1117; --panel: #161b22; --line: #30363d; --edge: #484f58; --muted: #8b949e;
      --text: #e6edf3; --blue: #58a6ff; --green: #3fb950; --red: #f85149; --amber: #d29922;
      --gray: #8b949e; --hover: #21262d; --chip: #21262d; --th: #161b22;
      --topbar: #010409; --topbar-muted: #8b949e; } }
    * { box-sizing: border-box; }
    body { margin: 0; overflow-x: hidden; color: var(--text); background: var(--bg);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    a { color: var(--blue); text-decoration: none; } a:hover { text-decoration: underline; }
    code { font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace;
      overflow-wrap: anywhere; }
    .topbar { min-height: 56px; display: flex; align-items: center; gap: 12px; padding: 0 24px;
      color: #ffffff; background: var(--topbar); border-bottom: 1px solid var(--line); }
    .topbar strong { font-size: 15px; } .topbar span { color: var(--topbar-muted); }
    .layout { max-width: 1320px; margin: 0 auto;
      display: grid; grid-template-columns: 220px minmax(0, 1fr); }
    aside { padding: 24px 16px; border-right: 1px solid var(--line);
      min-height: calc(100vh - 56px); }
    aside h2 { margin: 0 8px 10px; font-size: 12px; color: var(--muted);
      text-transform: uppercase; }
    aside a { display: block; padding: 7px 8px; color: var(--text); border-radius: 6px; }
    aside a:hover { background: var(--hover); text-decoration: none; }
    main { min-width: 0; padding: 28px 32px 56px; }
    h1 { margin: 0; font-size: 24px; } h2 { margin: 30px 0 12px; font-size: 18px; }
    .subhead { margin: 5px 0 0; color: var(--muted); overflow-wrap: anywhere; }
    .st { display: inline-flex; align-items: center; justify-content: center;
      width: 16px; height: 16px; flex: 0 0 16px; border-radius: 50%;
      color: #ffffff; font-size: 10px; font-weight: 700; line-height: 1; }
    .st.succeeded { background: var(--green); } .st.succeeded::after { content: "\\2713"; }
    .st.failed { background: var(--red); } .st.failed::after { content: "\\2715"; }
    .st.cancelled { background: var(--gray); } .st.cancelled::after { content: "\\2715"; }
    .st.skipped { background: var(--gray); } .st.skipped::after { content: "/"; }
    .st.pending { background: transparent; border: 2px solid var(--amber); }
    .st.running { background: transparent; border: 2px solid var(--amber);
      border-top-color: transparent; animation: st-spin 1s linear infinite; }
    @keyframes st-spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .st.running { animation: none; } }
    .st-line { display: inline-flex; align-items: center; gap: 7px;
      font-size: 12px; font-weight: 600; }
    .st-line.succeeded { color: var(--green); } .st-line.failed { color: var(--red); }
    .st-line.running, .st-line.pending { color: var(--amber); }
    .st-line.skipped, .st-line.cancelled { color: var(--gray); }
    .run-head { display: flex; align-items: flex-start; gap: 14px; }
    .run-head .st { width: 28px; height: 28px; flex-basis: 28px;
      font-size: 15px; border-width: 3px; }
    .run-summary { display: grid; grid-template-columns: repeat(3, minmax(130px, 1fr));
      margin-top: 18px; background: var(--panel); border: 1px solid var(--line);
      border-radius: 6px; }
    .metric { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line); }
    .metric:last-child { border-right: 0; }
    .metric span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; }
    .metric strong { overflow-wrap: anywhere; }
    .job-graph { display: flex; align-items: stretch; margin: 0; padding: 12px 4px;
      list-style: none; overflow-x: auto; }
    .job-node { position: relative; display: flex; align-items: center; gap: 10px;
      min-width: 158px; padding: 11px 14px; border: 1px solid var(--line);
      border-radius: 8px; background: var(--panel); }
    .job-node + .job-node { margin-left: 34px; }
    .job-node + .job-node::before { content: ""; position: absolute; top: 50%; left: -35px;
      width: 34px; height: 2px; background: var(--edge); }
    .job-node strong { display: block; font-size: 13px; }
    .job-node .sub, .matrix-chip .sub { display: block; color: var(--muted); font-size: 11px; }
    .matrix-group + .matrix-group { margin-top: 16px; }
    .matrix-group h3 { margin: 0 0 8px; font-size: 13px; color: var(--muted);
      font-weight: 600; }
    .chip-grid { display: flex; flex-wrap: wrap; gap: 10px; }
    .matrix-chip { display: flex; align-items: center; gap: 10px; min-width: 200px;
      padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px;
      background: var(--panel); }
    .matrix-chip strong { display: block; font-size: 13px; }
    .table-wrap { max-width: 100%; overflow-x: auto; border: 1px solid var(--line);
      border-radius: 6px; background: var(--panel); }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 9px 12px; border-bottom: 1px solid var(--line);
      text-align: left; vertical-align: top; }
    th { background: var(--th); color: var(--muted); font-size: 12px; font-weight: 600; }
    tr:last-child td { border-bottom: 0; }
    td code { white-space: nowrap; }
    .iter-group { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
    .iter-group + .iter-group { margin-top: 12px; }
    .iter-group > summary { display: flex; align-items: center; gap: 10px;
      padding: 12px 14px; cursor: pointer; font-weight: 600; }
    .iter-group > summary::before { content: "\\25B8"; color: var(--muted); }
    .iter-group[open] > summary::before { content: "\\25BE"; }
    .iter-group > summary .count { color: var(--muted); font-weight: 400; font-size: 12px; }
    .attempt { border-top: 1px solid var(--line); }
    .attempt > summary { display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
      min-height: 48px; padding: 10px 14px; cursor: pointer; }
    .attempt > summary code { min-width: 168px; max-width: 100%; }
    .attempt .grow { flex: 1 1 160px; min-width: 0; color: var(--muted); }
    .attempt .score { font-weight: 600; }
    .attempt .dur { color: var(--muted); font-size: 12px; min-width: 56px; text-align: right; }
    .details-body { padding: 0 14px 14px 40px; }
    .meta { display: grid; grid-template-columns: repeat(4, minmax(100px, 1fr));
      gap: 10px; margin: 4px 0 14px; }
    .meta div { min-width: 0; }
    .meta span { display: block; color: var(--muted); font-size: 11px; }
    .meta strong { overflow-wrap: anywhere; }
    .pipeline { display: flex; align-items: stretch; overflow-x: auto; padding: 6px 2px 8px; }
    .step-node { position: relative; display: flex; align-items: center; gap: 9px;
      min-width: 140px; padding: 8px 11px; border: 1px solid var(--line);
      border-radius: 6px; background: var(--chip); }
    .step-node + .step-node { margin-left: 26px; }
    .step-node + .step-node::before { content: ""; position: absolute; top: 50%; left: -27px;
      width: 26px; height: 2px; background: var(--edge); }
    .step-node strong { display: block; font-size: 12px; }
    .step-node span { display: block; color: var(--muted); font-size: 11px; }
    .empty { margin: 0; padding: 16px; color: var(--muted); border: 1px dashed var(--line);
      border-radius: 6px; background: var(--panel); }
    .footnote { margin-top: 30px; color: var(--muted); font-size: 12px; }
    .report { max-width: 960px; margin: 0 auto; padding: 28px 20px 56px; }
    .artifact-list { margin: 0; padding-left: 20px; }
    .artifact-list li { margin: 4px 0; }
    @media (max-width: 820px) {
      .layout { display: block; width: 100%; max-width: 100%; }
      aside { width: 100%; max-width: 100%; min-width: 0; min-height: auto;
        border-right: 0; border-bottom: 1px solid var(--line); }
      aside nav { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      aside a { min-width: 0; overflow-wrap: anywhere; }
      main { width: 100%; max-width: 100%; padding: 24px 16px 48px; }
      main section { width: 100%; max-width: 100%; min-width: 0; }
      .table-wrap, .iter-group { width: 100%; min-width: 0; }
      .run-summary, .meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric:nth-child(2) { border-right: 0; }
      .metric:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
      .attempt > summary code { min-width: 0; flex: 1 1 140px; }
      .attempt .dur { min-width: 0; text-align: left; }
      .matrix-chip { min-width: 0; flex: 1 1 46%; }
    }
"""


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
    attempts = _render_attempt_groups(state["attempts"], now)
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
        name
        for name in PUBLIC_BUNDLE_FILES
        if (run_dir / name).is_file() and not (run_dir / name).is_symlink()
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
    for name in PUBLIC_BUNDLE_FILES:
        if name not in available:
            continue
        source = run_dir / name
        target = destination / name
        temporary = destination / f".{name}.tmp"
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


def _render_iterations(values: object, now: datetime | None) -> str:
    iterations = _sequence(values)
    if not iterations:
        return ""
    rows = []
    for value in iterations:
        iteration = _mapping(value)
        rows.append(
            "<tr>"
            f"<td>{_h(iteration.get('iteration'))}</td>"
            f"<td>{_status_line(_safe_status(iteration.get('status')))}</td>"
            f"<td>{_h(iteration.get('attempt_count', '-'))}</td>"
            f"<td>{_h(_duration_text(iteration, now) or '-')}</td>"
            f"<td>{_policy_text(iteration, now)}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Iteration</th><th>Status</th><th>Attempts</th><th>Duration</th><th>After-iteration policies</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _policy_text(iteration: Mapping[str, Any], now: datetime | None) -> str:
    parts = []
    for value in _sequence(iteration.get("policies")):
        policy = _mapping(value)
        text = f"{policy.get('name', 'policy')}: {_label(_safe_status(policy.get('status')))}"
        extras = []
        kill_count = policy.get("kill_count")
        if isinstance(kill_count, int) and kill_count:
            extras.append(f"kill {kill_count}")
        migration_count = policy.get("migration_count")
        if isinstance(migration_count, int) and migration_count:
            extras.append(f"migrate {migration_count}")
        if policy.get("stop") is True:
            extras.append("stop")
        duration = _duration_text(policy, now)
        if duration:
            extras.append(duration)
        if extras:
            text += f" ({' · '.join(extras)})"
        parts.append(_h(text))
    return ", ".join(parts) or "-"


def _render_matrix(
    values: object,
    attempts_by_id: Mapping[str, Mapping[str, Any]],
    now: datetime | None,
) -> str:
    matrix = _sequence(values)
    if not matrix:
        return '<p class="empty">No island attempts recorded yet.</p>'
    groups: dict[object, list[Mapping[str, Any]]] = {}
    order: list[object] = []
    for value in matrix:
        cell = _mapping(value)
        iteration = cell.get("iteration")
        if iteration not in groups:
            groups[iteration] = []
            order.append(iteration)
        groups[iteration].append(cell)
    sections = []
    for iteration in order:
        chips = []
        for cell in groups[iteration]:
            counts = _mapping(cell.get("counts"))
            status = _safe_status(cell.get("status"))
            attempt_ids = _sequence(cell.get("attempt_ids"))
            members = [attempts_by_id.get(str(attempt_id)) for attempt_id in attempt_ids]
            sub_parts = [
                f"{len(attempt_ids)} attempts",
                f"{counts.get('accepted', 0)} accepted",
                f"{counts.get('quarantined', 0)} quarantined",
            ]
            duration = _span_duration(members, now)
            if duration:
                sub_parts.append(duration)
            chips.append(
                f'<div class="matrix-chip">{_status_icon(status)}'
                f"<div><strong>island {_h(cell.get('island'))}</strong>"
                f'<span class="sub">{_h(" · ".join(sub_parts))}</span></div></div>'
            )
        sections.append(
            f'<section class="matrix-group"><h3>Iteration {_h(iteration)}</h3>'
            f'<div class="chip-grid">{"".join(chips)}</div></section>'
        )
    return "".join(sections)


def _render_attempt_groups(values: object, now: datetime | None) -> str:
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
    sections = []
    for iteration in order:
        members = groups[iteration]
        active = any(
            _safe_status(member.get("status")) in {"running", "failed"} for member in members
        )
        open_attr = " open" if iteration == latest or active else ""
        title = f"Iteration {iteration}" if iteration is not None else "Other attempts"
        body = "".join(_render_attempt(member, now) for member in members)
        sections.append(
            f'<details class="iter-group"{open_attr}><summary>{_h(title)}'
            f'<span class="count">{len(members)} attempts</span></summary>{body}</details>'
        )
    return "".join(sections)


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


def _render_coverage(value: object) -> str:
    coverage = _mapping(value)
    if not coverage:
        return '<p class="empty">Trace coverage is finalized when the run ends.</p>'
    rows = [
        ("Invocations", coverage.get("total", 0)),
        ("Complete capture", coverage.get("capture_complete", 0)),
        ("Degraded capture", coverage.get("capture_degraded", 0)),
        ("Interrupted capture", coverage.get("capture_interrupted", 0)),
        ("Gateway completed", coverage.get("gateway_completed", 0)),
        ("Gateway failed", coverage.get("gateway_failed", 0)),
        ("Gateway interrupted", coverage.get("gateway_interrupted", 0)),
    ]
    body = "".join(f"<tr><td>{_h(name)}</td><td>{_h(count)}</td></tr>" for name, count in rows)
    return f'<div class="table-wrap"><table><thead><tr><th>Measure</th><th>Count</th></tr></thead><tbody>{body}</tbody></table></div>'


def _status_icon(status: str) -> str:
    return f'<span class="st {_h(status)}" role="img" aria-label="{_h(_label(status))}"></span>'


def _status_line(status: str) -> str:
    return f'<span class="st-line {_h(status)}">{_status_icon(status)}{_h(_label(status))}</span>'


def _label(value: object) -> str:
    return str(value or "pending").capitalize()


def _accepted_text(value: object) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "-"


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_ts(moment: datetime) -> str:
    suffix = " UTC" if moment.tzinfo is not None else ""
    return moment.strftime("%Y-%m-%d %H:%M") + suffix


def _format_duration(seconds: float) -> str:
    total = int(seconds + 0.5)
    if total < 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _duration_text(record: Mapping[str, Any], now: datetime | None) -> str:
    started = _parse_ts(_mapping(record.get("started")).get("ts"))
    if started is None:
        return ""
    end = _parse_ts(_mapping(record.get("finished")).get("ts"))
    if end is None:
        if record.get("status") != "running" or now is None:
            return ""
        end = now
    try:
        seconds = (end - started).total_seconds()
    except TypeError:
        return ""
    return _format_duration(seconds) if seconds >= 0 else ""


def _span_duration(
    members: Sequence[Mapping[str, Any] | None], now: datetime | None
) -> str:
    starts = []
    ends = []
    running = False
    for member in members:
        record = _mapping(member)
        if not record:
            continue
        started = _parse_ts(_mapping(record.get("started")).get("ts"))
        finished = _parse_ts(_mapping(record.get("finished")).get("ts"))
        if started is not None:
            starts.append(started)
        if finished is not None:
            ends.append(finished)
        if record.get("status") == "running":
            running = True
    if not starts:
        return ""
    try:
        if running and now is not None:
            end = now
        elif ends:
            end = max(ends)
        else:
            return ""
        seconds = (end - min(starts)).total_seconds()
    except TypeError:
        return ""
    return _format_duration(seconds) if seconds >= 0 else ""


def _safe_status(value: object) -> str:
    text = str(value or "pending")
    return (
        text
        if text in {"pending", "running", "succeeded", "failed", "skipped", "cancelled"}
        else "pending"
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _md(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


__all__: list[str] = []
