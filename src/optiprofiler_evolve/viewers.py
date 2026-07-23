"""Server-free views derived only from the sanitized public event projection."""

from __future__ import annotations

import html
import shutil
from collections.abc import Mapping
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


def render_status(events_path: Path, destination: Path) -> dict[str, Any]:
    """Atomically render the local Actions-style status view."""

    state = write_run_state(events_path, destination.parent / "public_run_state.json")
    run = state["run"]
    run_status = _safe_status(run.get("status"))
    refresh = (
        '<meta http-equiv="refresh" content="5">' if run_status in {"pending", "running"} else ""
    )
    phases = _render_phases(state["phases"])
    iterations = _render_iterations(state["iterations"])
    matrix = _render_matrix(state["matrix"])
    attempts = _render_attempts(state["attempts"])
    roles = _render_roles(state["roles"])
    coverage = _render_coverage(state.get("trace_coverage"))
    best_candidate = run.get("best_candidate_id") or "Not selected"
    updated = state.get("updated_at") or "No events"

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptiProfiler Evolve Status</title>
  <style>
    :root {{ color-scheme: light; --bg: #f6f8fa; --panel: #ffffff; --line: #d0d7de; --muted: #59636e; --text: #1f2328; --blue: #0969da; --green: #1a7f37; --red: #cf222e; --amber: #9a6700; --gray: #6e7781; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; overflow-x: hidden; color: var(--text); background: var(--bg); font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; letter-spacing: 0; }}
    a {{ color: var(--blue); text-decoration: none; }} a:hover {{ text-decoration: underline; }}
    code {{ font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }}
    .topbar {{ min-height: 56px; display: flex; align-items: center; gap: 12px; padding: 0 24px; color: #ffffff; background: #24292f; border-bottom: 1px solid #57606a; }}
    .topbar strong {{ font-size: 15px; }} .topbar span {{ color: #afb8c1; }}
    .layout {{ max-width: 1320px; margin: 0 auto; display: grid; grid-template-columns: 220px minmax(0, 1fr); }}
    aside {{ padding: 24px 16px; border-right: 1px solid var(--line); min-height: calc(100vh - 56px); }}
    aside h2 {{ margin: 0 8px 10px; font-size: 12px; color: var(--muted); text-transform: uppercase; }}
    aside a {{ display: block; padding: 7px 8px; color: var(--text); border-radius: 6px; }} aside a:hover {{ background: #eaeef2; text-decoration: none; }}
    main {{ min-width: 0; padding: 28px 32px 56px; }}
    h1 {{ margin: 0; font-size: 24px; }} h2 {{ margin: 30px 0 12px; font-size: 18px; }} h3 {{ margin: 0; font-size: 14px; }}
    .subhead {{ margin: 5px 0 0; color: var(--muted); overflow-wrap: anywhere; }}
    .run-summary {{ display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); margin-top: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }}
    .metric {{ min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line); }} .metric:last-child {{ border-right: 0; }}
    .metric span {{ display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; }} .metric strong {{ overflow-wrap: anywhere; }}
    .status {{ display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; font-weight: 600; text-transform: capitalize; }}
    .status::before {{ content: ""; width: 9px; height: 9px; flex: 0 0 9px; border-radius: 50%; background: var(--gray); box-shadow: inset 0 0 0 1px rgba(0,0,0,.12); }}
    .status.succeeded {{ color: var(--green); }} .status.succeeded::before {{ background: var(--green); }}
    .status.failed {{ color: var(--red); }} .status.failed::before {{ background: var(--red); }}
    .status.running {{ color: var(--blue); }} .status.running::before {{ background: var(--blue); }}
    .status.cancelled, .status.skipped {{ color: var(--gray); }} .status.pending {{ color: var(--amber); }} .status.pending::before {{ background: var(--amber); }}
    .phase-flow {{ display: flex; gap: 0; margin: 0; padding: 0; list-style: none; overflow-x: auto; }}
    .phase-flow li {{ position: relative; min-width: 132px; padding: 12px 28px 12px 12px; background: var(--panel); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
    .phase-flow li:first-child {{ border-left: 1px solid var(--line); border-radius: 6px 0 0 6px; }} .phase-flow li:last-child {{ border-right: 1px solid var(--line); border-radius: 0 6px 6px 0; }}
    .phase-flow li:not(:last-child)::after {{ content: ">"; position: absolute; right: 9px; top: 20px; color: var(--gray); font-weight: 700; }}
    .phase-flow strong {{ display: block; margin-bottom: 4px; font-size: 13px; }}
    .table-wrap {{ max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }}
    table {{ width: 100%; border-collapse: collapse; }} th, td {{ padding: 9px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ background: #f6f8fa; color: var(--muted); font-size: 12px; font-weight: 600; }} tr:last-child td {{ border-bottom: 0; }}
    .item-list {{ max-width: 100%; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }}
    details + details {{ border-top: 1px solid var(--line); }} summary {{ display: flex; align-items: center; flex-wrap: wrap; gap: 12px; min-height: 48px; padding: 10px 14px; cursor: pointer; }}
    summary code {{ min-width: 168px; max-width: 100%; }} summary .grow {{ flex: 1 1 180px; min-width: 0; }}
    .details-body {{ padding: 0 14px 14px 38px; }}
    .meta {{ display: grid; grid-template-columns: repeat(4, minmax(100px, 1fr)); gap: 10px; margin: 4px 0 14px; }}
    .meta div {{ min-width: 0; }} .meta span {{ display: block; color: var(--muted); font-size: 11px; }} .meta strong {{ overflow-wrap: anywhere; }}
    .pipeline {{ display: flex; align-items: stretch; gap: 6px; overflow-x: auto; padding-bottom: 3px; }}
    .step {{ min-width: 126px; padding: 8px 10px; border-left: 3px solid var(--line); background: #f6f8fa; }} .step.succeeded {{ border-color: var(--green); }} .step.failed {{ border-color: var(--red); }} .step.running {{ border-color: var(--blue); }}
    .step strong {{ display: block; font-size: 12px; }} .step span {{ color: var(--muted); font-size: 11px; }}
    .empty {{ margin: 0; padding: 16px; color: var(--muted); border: 1px dashed var(--line); border-radius: 6px; background: var(--panel); }}
    .footnote {{ margin-top: 30px; color: var(--muted); font-size: 12px; }}
    @media (max-width: 820px) {{ .layout {{ display: block; width: 100%; max-width: 100%; }} aside {{ width: 100%; max-width: 100%; min-width: 0; min-height: auto; border-right: 0; border-bottom: 1px solid var(--line); }} aside nav {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }} aside a {{ min-width: 0; overflow-wrap: anywhere; }} main {{ width: 100%; max-width: 100%; padding: 24px 16px 48px; }} main section {{ width: 100%; max-width: 100%; min-width: 0; }} .table-wrap, .item-list {{ width: 100%; min-width: 0; }} .run-summary, .meta {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .metric:nth-child(2) {{ border-right: 0; }} .metric:nth-child(-n+2) {{ border-bottom: 1px solid var(--line); }} summary code {{ min-width: 0; flex: 1 1 150px; }} summary .grow {{ flex-basis: 160px; }} }}
  </style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Run workflow</span></header>
  <div class="layout">
    <aside><h2>Run details</h2><nav><a href="#summary">Summary</a><a href="#workflow">Workflow</a><a href="#matrix">Island matrix</a><a href="#attempts">Attempts</a><a href="#roles">Research roles</a><a href="#coverage">Trace coverage</a></nav></aside>
    <main>
      <section id="summary">
        <h1>Evolution run</h1><p class="subhead">Updated {_h(updated)} from public event {_h(state["last_seq"])}.</p>
        <div class="run-summary">
          <div class="metric"><span>Status</span>{_status_badge(run_status)}</div>
          <div class="metric"><span>Best candidate</span><strong><code>{_h(best_candidate)}</code></strong></div>
          <div class="metric"><span>Iterations</span><strong>{len(state["iterations"])}</strong></div>
          <div class="metric"><span>Attempts</span><strong>{len(state["attempts"])}</strong></div>
        </div>
      </section>
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
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OptiProfiler Evolve Public Report</title><style>body{{max-width:920px;margin:40px auto;padding:0 20px;color:#1f2328;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}}h1{{font-size:24px}}.summary{{padding:16px;border:1px solid #d0d7de;border-radius:6px;background:#f6f8fa}}a{{color:#0969da}}code{{overflow-wrap:anywhere}}</style></head>
<body><h1>Evolution public report</h1><div class="summary"><p>Status: <strong>{_h(run.get("status", "pending"))}</strong></p><p>Best candidate: <code>{_h(run.get("best_candidate_id") or "not selected")}</code></p><p>Public attempts recorded: <strong>{len(_sequence(state.get("attempts")))}</strong></p></div><h2>Public artifacts</h2><ul><li><a href="status.html">Actions-style run status</a></li><li><a href="PUBLIC_REPORT.md">Markdown run summary</a></li><li><a href="public_events.jsonl">Public event ledger</a></li><li><a href="public_run_state.json">Public run state</a></li></ul><p>This report deliberately excludes validation and hidden scores, reviewer findings, provider details, and raw traces.</p></body></html>
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


def _render_phases(values: object) -> str:
    phases = _sequence(values)
    if not phases:
        return '<p class="empty">No phases recorded yet.</p>'
    items = []
    for value in phases:
        phase = _mapping(value)
        status = _safe_status(phase.get("status"))
        items.append(
            f"<li><strong>{_h(phase.get('name', 'phase'))}</strong>{_status_badge(status)}</li>"
        )
    return f'<ol class="phase-flow">{"".join(items)}</ol>'


def _render_iterations(values: object) -> str:
    iterations = _sequence(values)
    if not iterations:
        return ""
    rows = []
    for value in iterations:
        iteration = _mapping(value)
        policies = _sequence(iteration.get("policies"))
        policy_text = (
            ", ".join(
                f"{_h(_mapping(policy).get('name', 'policy'))}:"
                f"{_h(_mapping(policy).get('status', 'pending'))}"
                for policy in policies
            )
            or "-"
        )
        rows.append(
            "<tr>"
            f"<td>{_h(iteration.get('iteration'))}</td>"
            f"<td>{_status_badge(_safe_status(iteration.get('status')))}</td>"
            f"<td>{_h(iteration.get('attempt_count', '-'))}</td>"
            f"<td>{policy_text}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Iteration</th><th>Status</th><th>Attempts</th><th>After-iteration policies</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _render_matrix(values: object) -> str:
    matrix = _sequence(values)
    if not matrix:
        return '<p class="empty">No island attempts recorded yet.</p>'
    rows = []
    for value in matrix:
        cell = _mapping(value)
        counts = _mapping(cell.get("counts"))
        rows.append(
            "<tr>"
            f"<td>{_h(cell.get('iteration'))}</td>"
            f"<td>{_h(cell.get('island'))}</td>"
            f"<td>{_status_badge(_safe_status(cell.get('status')))}</td>"
            f"<td>{len(_sequence(cell.get('attempt_ids')))}</td>"
            f"<td>{_h(counts.get('accepted', 0))}</td>"
            f"<td>{_h(counts.get('quarantined', 0))}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Iteration</th><th>Island</th><th>Status</th><th>Attempts</th><th>Accepted</th><th>Quarantined</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _render_attempts(values: object) -> str:
    attempts = _sequence(values)
    if not attempts:
        return '<p class="empty">No candidate attempts recorded yet.</p>'
    rendered = []
    for value in attempts:
        attempt = _mapping(value)
        status = _safe_status(attempt.get("status"))
        score = attempt.get("public_score")
        score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "-"
        steps = _sequence(attempt.get("steps"))
        pipeline = "".join(_render_step(_mapping(step)) for step in steps)
        if not pipeline:
            pipeline = '<div class="step pending"><strong>Waiting</strong><span>No steps recorded</span></div>'
        worker = _mapping(attempt.get("worker"))
        review = _mapping(attempt.get("integrity_review"))
        details_open = " open" if status in {"failed", "cancelled"} else ""
        rendered.append(
            f"<details{details_open}><summary>{_status_badge(status)}"
            f"<code>{_h(attempt.get('attempt_id'))}</code>"
            '<span class="grow">'
            f"Island {_h(attempt.get('island', '-'))}, "
            f"iteration {_h(attempt.get('iteration', '-'))}"
            "</span>"
            f"<span>Public {_h(score_text)}</span></summary>"
            '<div class="details-body"><div class="meta">'
            f"<div><span>Parent</span><strong><code>{_h(attempt.get('parent_id', '-'))}"
            "</code></strong></div>"
            f"<div><span>Accepted</span><strong>{_h(attempt.get('accepted', '-'))}"
            "</strong></div>"
            f"<div><span>Worker</span><strong>{_h(worker.get('status', 'pending'))}"
            "</strong></div>"
            "<div><span>Integrity gate</span><strong>"
            f"{_h(review.get('gate', review.get('status', 'pending')))}"
            "</strong></div>"
            f'</div><div class="pipeline">{pipeline}</div></div></details>'
        )
    return f'<div class="item-list">{"".join(rendered)}</div>'


def _render_step(step: Mapping[str, Any]) -> str:
    status = _safe_status(step.get("status"))
    detail = step.get("verdict") or step.get("outcome") or status
    return (
        f'<div class="step {_h(status)}"><strong>{_h(step.get("name", "step"))}'
        f"</strong><span>{_h(detail)}</span></div>"
    )


def _render_roles(values: object) -> str:
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
            f"<td>{_status_badge(_safe_status(role.get('status')))}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Job</th><th>Role</th><th>Phase</th><th>Island</th><th>Status</th></tr></thead><tbody>'
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


def _status_badge(status: str) -> str:
    return f'<span class="status {_h(status)}">{_h(status)}</span>'


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
