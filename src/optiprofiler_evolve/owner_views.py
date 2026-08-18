"""Private owner dashboard rendered from the raw event ledger and run evidence.

Everything in this module is controller-side output for the run owner. Pages
are written to ``run_dir/status.html`` and ``run_dir/owner/`` only — never into
``run_dir/public`` and never into any worker-visible directory. The public
sanitized pages remain the responsibility of :mod:`.viewers`.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ._owner_evidence import (
    DIFF_PREVIEW_LINES,
    PHASE_EVENT_LIMIT,
    compact_text,
    parse_transcript,
    phase_artifacts,
    phase_component,
    preview_stream,
    read_json,
    resolve_recorded_path,
    resolve_role_output,
    safe_page_name,
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

# Static marker for historical *_started ledger rows; never animated.
_STARTED_BADGE = (
    '<span class="st-line started">'
    '<span class="st started" role="img" aria-label="Started"></span>Started</span>'
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
    .population-summary { grid-template-columns: repeat(4, minmax(120px, 1fr)); }
    .attempt-link { display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
      min-height: 44px; padding: 10px 14px; border-top: 1px solid var(--line);
      color: var(--text); }
    .attempt-link:hover { background: var(--hover); text-decoration: none; }
    .attempt-link code { min-width: 168px; }
    .attempt-link .grow { flex: 1 1 120px; min-width: 0; color: var(--muted); }
    .attempt-link .score { font-weight: 600; }
    .attempt-link .dur { color: var(--muted); font-size: 12px; }
    .score-summary { grid-template-columns: repeat(4, minmax(120px, 1fr)); }
    .score-chart { max-width: 100%; overflow-x: auto; border: 1px solid var(--line);
      border-radius: 8px; background: var(--panel); }
    .score-chart-head { display: flex; align-items: baseline; justify-content: space-between;
      gap: 12px; padding: 12px 16px 0; }
    .score-chart-head strong { font-size: 13px; }
    .score-chart-head span { color: var(--muted); font-size: 11px; }
    .score-toolbar { display: flex; align-items: center; gap: 8px; margin: 10px 0; }
    .score-toolbar label { color: var(--muted); font-size: 12px; }
    .score-toolbar select { padding: 5px 28px 5px 9px; border: 1px solid var(--line);
      border-radius: 6px; color: var(--text); background: var(--panel); font: inherit; }
    .score-view[hidden] { display: none; }
    .score-chart svg { display: block; min-width: 100%; height: 330px; }
    .score-chart .grid { stroke: var(--line); stroke-width: 1; }
    .score-chart .axis-label { fill: var(--muted); font-size: 11px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .score-chart .iteration-label { fill: var(--text); font-size: 11px;
      font-weight: 600; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .score-chart .metric-line { fill: none; stroke-width: 2; stroke-linecap: round;
      stroke-linejoin: round; }
    .score-chart .metric-line.public { stroke: var(--blue); }
    .score-chart .metric-line.validation { stroke: var(--green); }
    .score-chart .metric-line.hidden { stroke: var(--amber); }
    .score-chart .metric-point { stroke: var(--panel); stroke-width: 1.5; }
    .score-chart .metric-point.public { fill: var(--blue); }
    .score-chart .metric-point.validation { fill: var(--green); }
    .score-chart .metric-point.hidden { fill: var(--amber); }
    .score-chart .metric-point.rejected,
    .score-chart .metric-point.quarantined { opacity: .5; }
    .score-chart .tie-line { stroke: var(--muted); stroke-width: 1;
      stroke-dasharray: 5 5; }
    .score-legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 8px 0; }
    .score-legend span { display: inline-flex; align-items: center; gap: 6px;
      color: var(--muted); font-size: 12px; }
    .score-swatch { width: 10px; height: 10px; border-radius: 50%; }
    .score-swatch.public { background: var(--blue); }
    .score-swatch.validation { background: var(--green); }
    .score-swatch.hidden { background: var(--amber); }
    .lineage-chart { max-width: 100%; max-height: 680px; overflow: auto;
      border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
    .lineage-view[hidden] { display: none; }
    .lineage-chart svg { display: block; min-width: 100%; height: auto; min-height: 250px; }
    .lineage-grid { stroke: var(--line); stroke-width: 1; stroke-dasharray: 2 5; opacity: .65; }
    .lineage-seed-rail { stroke: var(--edge); stroke-width: 1; stroke-dasharray: 3 5;
      opacity: .65; }
    .lineage-edge { stroke: var(--edge); stroke-width: 1.5; fill: none; }
    .lineage-edge.external, .lineage-edge.migration { stroke-dasharray: 5 4; }
    .lineage-edge.migration { stroke: var(--amber); stroke-width: 2; }
    .lineage-island-band { fill: var(--bg); stroke: var(--line); stroke-width: 1; opacity: .7; }
    .lineage-island-label { fill: var(--text); font-size: 11px; font-weight: 700; }
    .lineage-seed-node { fill: var(--panel); stroke: var(--muted); stroke-width: 1.5; }
    .lineage-seed-label { fill: var(--muted); font-size: 10px; font-weight: 600; }
    .lineage-key { display: inline-block; width: 24px; height: 0;
      border-top: 2px solid var(--edge); }
    .lineage-key.seed { border-top-style: dashed; opacity: .7; }
    .lineage-key.migration { border-top-color: var(--amber); border-top-style: dashed; }
    .lineage-node { fill: var(--panel); stroke: var(--blue); stroke-width: 2; }
    .lineage-node.retained { fill: var(--blue); }
    .lineage-node.quarantined { stroke: var(--amber); }
    .lineage-node.rejected, .lineage-node.failed { stroke: var(--red); }
    .lineage-node-label, .lineage-edge-label { fill: var(--muted); font-size: 10px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .lineage-edge-label { font-size: 9px; }
    .score-outcome { white-space: nowrap; font-size: 12px; font-weight: 600; }
    .score-outcome.retained { color: var(--green); }
    .score-outcome.not-retained { color: var(--muted); }
    .score-outcome.rejected { color: var(--red); }
    .score-outcome.quarantined { color: var(--amber); }
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
    .wf-controls { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; }
    .wf-controls button { padding: 4px 12px; border: 1px solid var(--line);
      border-radius: 6px; background: var(--panel); color: var(--text);
      font: inherit; font-size: 12px; cursor: pointer; }
    .wf-controls button:hover { background: var(--hover); }
    .wf-level { color: var(--muted); font-size: 12px; min-width: 44px; }
    .wf-canvas { overflow: auto; max-height: 420px; border: 1px solid var(--line);
      border-radius: 8px; background: var(--bg); cursor: grab; }
    .wf-canvas.dragging { cursor: grabbing; user-select: none; }
    .wf-inner { display: inline-block; min-width: 100%; padding: 12px 16px; }
    .wf-inner .job-graph { overflow: visible; padding: 4px 2px; }
    .job-graph a.job-node { color: var(--text); }
    .job-graph a.job-node:hover { border-color: var(--blue); text-decoration: none; }
    .phase-events td { font-size: 12px; }
    .st.started { background: transparent; border: 2px solid var(--edge);
      animation: none; }
    .st-line.started { color: var(--muted); }
    .st.quarantined { background: var(--amber); animation: none; }
    .st.quarantined::after { content: "!"; }
    .st-line.quarantined { color: var(--amber); }
    @media (max-width: 820px) {
      .kv { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .population-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .score-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .attempt-link code { min-width: 0; flex: 1 1 140px; }
    }
"""

# Static pan/zoom controller for the owner workflow canvas. It interpolates no
# run data and talks to no network; the canvas degrades to a plain scrollable
# strip when scripting is unavailable. Owner pages only — the public bundle
# stays script-free and is test-pinned to stay that way.
_WF_SCRIPT = """<script>
(function () {
  "use strict";
  var canvas = document.getElementById("wf-canvas");
  var inner = document.getElementById("wf-inner");
  var level = document.getElementById("wf-level");
  if (!canvas || !inner) { return; }
  var scale = 1;
  var supportsZoom = "zoom" in inner.style;
  function apply() {
    if (supportsZoom) {
      inner.style.zoom = scale;
    } else {
      inner.style.transformOrigin = "0 0";
      inner.style.transform = "scale(" + scale + ")";
    }
    if (level) { level.textContent = Math.round(scale * 100) + "%"; }
  }
  function set(next) { scale = Math.min(2, Math.max(0.25, next)); apply(); }
  function fit() {
    if (supportsZoom) { inner.style.zoom = 1; } else { inner.style.transform = "none"; }
    var natural = inner.scrollWidth;
    var available = canvas.clientWidth - 2;
    set(natural > 0 ? Math.min(1, available / natural) : 1);
  }
  var buttons = document.querySelectorAll("[data-wf]");
  Array.prototype.forEach.call(buttons, function (button) {
    button.addEventListener("click", function () {
      var action = button.getAttribute("data-wf");
      if (action === "in") { set(scale * 1.25); }
      else if (action === "out") { set(scale / 1.25); }
      else if (action === "reset") { set(1); }
      else if (action === "fit") { fit(); }
    });
  });
  var drag = null;
  var suppressClick = false;
  canvas.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) { return; }
    drag = { x: event.clientX, y: event.clientY,
             left: canvas.scrollLeft, top: canvas.scrollTop, moved: false };
  });
  window.addEventListener("pointermove", function (event) {
    if (!drag) { return; }
    var dx = event.clientX - drag.x;
    var dy = event.clientY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 6) { return; }
    drag.moved = true;
    canvas.classList.add("dragging");
    canvas.scrollLeft = drag.left - dx;
    canvas.scrollTop = drag.top - dy;
  });
  window.addEventListener("pointerup", function () {
    if (drag && drag.moved) {
      suppressClick = true;
      window.setTimeout(function () { suppressClick = false; }, 0);
    }
    canvas.classList.remove("dragging");
    drag = null;
  });
  canvas.addEventListener("click", function (event) {
    if (suppressClick) { event.preventDefault(); event.stopPropagation(); }
  }, true);
})();
</script>"""

# Owner pages are served from a local, same-origin run directory. Polling the
# regenerated HTML and replacing only the dashboard root avoids a full browser
# navigation every five seconds. Public pages remain script-free/static.
_LIVE_REFRESH_HEAD = '<meta name="optiprofiler-evolve-live" content="1">'
_LIVE_REFRESH_SCRIPT = """<script>
(function () {
  "use strict";
  var marker = 'meta[name="optiprofiler-evolve-live"]';
  var rootSelector = "[data-live-root]";
  var stopped = false;

  function selectState(root) {
    var values = {};
    root.querySelectorAll("select[id]").forEach(function (select) {
      values[select.id] = select.value;
    });
    return values;
  }

  function detailsState(root) {
    return Array.prototype.map.call(root.querySelectorAll("details"), function (details) {
      return details.open;
    });
  }

  function restoreState(root, selects, details) {
    Object.keys(selects).forEach(function (id) {
      var select = root.querySelector("#" + CSS.escape(id));
      if (select && Array.prototype.some.call(select.options, function (option) {
        return option.value === selects[id];
      })) {
        select.value = selects[id];
      }
    });
    root.querySelectorAll("details").forEach(function (item, index) {
      if (index < details.length) { item.open = details[index]; }
    });
  }

  function activateScripts(root) {
    root.querySelectorAll("script").forEach(function (oldScript) {
      var script = document.createElement("script");
      Array.prototype.forEach.call(oldScript.attributes, function (attribute) {
        script.setAttribute(attribute.name, attribute.value);
      });
      script.textContent = oldScript.textContent;
      oldScript.replaceWith(script);
    });
  }

  async function refresh() {
    if (stopped) { return; }
    try {
      var response = await window.fetch(window.location.href, {cache: "no-store"});
      if (!response.ok) { throw new Error("dashboard refresh failed"); }
      var nextDocument = new DOMParser().parseFromString(await response.text(), "text/html");
      var currentRoot = document.querySelector(rootSelector);
      var nextRoot = nextDocument.querySelector(rootSelector);
      if (!currentRoot || !nextRoot) { throw new Error("dashboard root unavailable"); }

      if (currentRoot.innerHTML !== nextRoot.innerHTML) {
        var scrollX = window.scrollX;
        var scrollY = window.scrollY;
        var selects = selectState(currentRoot);
        var details = detailsState(currentRoot);
        var replacement = document.importNode(nextRoot, true);
        currentRoot.replaceWith(replacement);
        restoreState(replacement, selects, details);
        activateScripts(replacement);
        window.scrollTo(scrollX, scrollY);
      }
      document.title = nextDocument.title;
      if (!nextDocument.querySelector(marker)) { stopped = true; }
    } catch (error) {
      // Keep the last complete dashboard visible and retry after transient I/O.
    }
    if (!stopped) { window.setTimeout(refresh, 5000); }
  }

  if (window.location.protocol === "http:" || window.location.protocol === "https:") {
    window.setTimeout(refresh, 5000);
  } else {
    // Direct file:// viewing cannot reliably fetch regenerated sibling files.
    window.setTimeout(function () { window.location.reload(); }, 5000);
  }
})();
</script>"""


def _live_refresh(status: str) -> tuple[str, str]:
    if status in {"pending", "running"}:
        return _LIVE_REFRESH_HEAD, _LIVE_REFRESH_SCRIPT
    return "", ""


def render_owner_views(events_path: Path, run_dir: Path, *, final: bool = False) -> None:
    """Render the owner index, per-attempt and per-role pages, atomically."""

    events = read_events(events_path)
    state = rebuild_run_state(events_path)
    details = _collect_private_details(events)
    now = _parse_ts(state.get("updated_at"))

    owner_dir = run_dir / "owner"
    (owner_dir / "attempts").mkdir(parents=True, exist_ok=True)
    (owner_dir / "roles").mkdir(parents=True, exist_ok=True)
    (owner_dir / "phases").mkdir(parents=True, exist_ok=True)

    for value in _sequence(state.get("phases")):
        phase = _mapping(value)
        page_name = safe_page_name(phase.get("name"))
        if page_name is None:
            continue
        page = owner_dir / "phases" / f"{page_name}.html"
        terminal = _safe_status(phase.get("status")) not in {"pending", "running"}
        if terminal and page.is_file() and not final:
            continue
        _atomic_write(
            page,
            _render_phase_page(phase, events, state, details, run_dir, now),
        )

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

    _atomic_write(
        run_dir / "status.html",
        _render_owner_index(state, details, run_dir, now),
    )
    if final:
        write_owner_manifest(state, run_dir)


def _collect_private_details(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Gather controller-private per-attempt and per-role fields from raw events."""

    attempts: dict[str, dict[str, Any]] = {}
    roles: dict[str, dict[str, Any]] = {}
    for position, event in enumerate(events, start=1):
        kind = str(event.get("kind", ""))
        scope = _mapping(event.get("scope"))
        data = _mapping(event.get("data"))
        event_order = event.get("seq")
        if not isinstance(event_order, int):
            event_order = position
        attempt_id = scope.get("attempt_id")
        if attempt_id is not None:
            record = attempts.setdefault(
                str(attempt_id),
                {"steps": {}, "review_attempts": [], "worker": {}},
            )
            if kind == "attempt_started":
                record["started_order"] = event_order
                for key in ("parent_id", "guidance"):
                    if key in data:
                        record[key] = data.get(key)
                if isinstance(data.get("worker"), str):
                    record["worker_name"] = data["worker"]
            elif kind == "attempt_finished":
                record.update({key: value for key, value in data.items() if key != "steps"})
                if _finite_score(data.get("validation_score")) is not None and data.get("valid"):
                    record["validation_observed_order"] = event_order
            elif kind == "worker_finished":
                record["worker"] = dict(data)
            elif kind == "step_finished":
                name = str(scope.get("step", "step"))
                metrics = _mapping(data.get("metrics"))
                record["steps"][name] = {
                    "metrics": metrics,
                    "artifacts": _sequence(data.get("artifacts")),
                    "error": data.get("error"),
                    "verdict": data.get("verdict"),
                }
                if _finite_score(metrics.get("public_score")) is not None:
                    record["public_observed_order"] = event_order
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
    state: Mapping[str, Any],
    details: Mapping[str, Any],
    run_dir: Path,
    now: datetime | None,
) -> str:
    run = _mapping(state.get("run"))
    run_status = _safe_status(run.get("status"))
    refresh_head, refresh_script = _live_refresh(run_status)
    head = _owner_run_head(run, run_status, state, now, run_dir)
    phases = _render_owner_phase_graph(state, now)
    iterations = _render_iterations(state.get("iterations"), now)
    attempts_by_id = {
        str(_mapping(value).get("attempt_id")): _mapping(value)
        for value in _sequence(state.get("attempts"))
    }
    matrix = _render_matrix(state.get("matrix"), attempts_by_id, now)
    population_policy = _render_population_policy(run_dir)
    score_timeline = _render_score_timeline(state, details, run_dir)
    lineage = _render_lineage(state, details, run_dir)
    attempts = _render_owner_attempt_groups(state.get("attempts"), details, now, run_dir=run_dir)
    roles = _render_owner_roles(state.get("roles"), now, details=details)
    coverage = _render_coverage(state.get("trace_coverage"))
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh_head}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptiProfiler Evolve Owner Console</title>
  <style>{_STYLE}{_OWNER_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Owner console</span></header>
  {_BANNER}
  <div class="layout" data-live-root>
    <aside><h2>Run details</h2><nav><a href="#summary">Summary</a><a href="#workflow">Workflow</a><a href="#matrix">Island matrix</a><a href="#population-policy">Population policy</a><a href="#score-timeline">Evolution metrics</a><a href="#lineage">Lineage</a><a href="#attempts">Attempts</a><a href="#roles">Agent jobs</a><a href="#coverage">Trace coverage</a></nav></aside>
    <main>
      <section id="summary">{head}</section>
      <section id="workflow"><h2>Workflow</h2>{phases}{iterations}</section>
      <section id="matrix"><h2>Island matrix</h2>{matrix}</section>
      <section id="population-policy"><h2>Population policy</h2>{population_policy}</section>
      <section id="score-timeline"><h2>Evolution metrics</h2>{score_timeline}</section>
      <section id="lineage"><h2>Candidate lineage</h2>{lineage}</section>
      <section id="attempts"><h2>Attempts</h2>{attempts}</section>
      <section id="roles"><h2>Trusted agent jobs</h2>{roles}</section>
      <section id="coverage"><h2>Agent trace coverage</h2>{coverage}</section>
      <p class="footnote">Private owner console generated from the raw event ledger. Every attempt and trusted agent job links to a detail page with full evidence.</p>
    </main>
  </div>
  {refresh_script}
</body>
</html>
"""
    return document


def _render_population_policy(run_dir: Path) -> str:
    provenance = _mapping(read_json(run_dir / "provenance.json"))
    components = _mapping(provenance.get("components"))
    retention = _mapping(components.get("retention"))
    sampler = _mapping(components.get("parent_sampler"))
    retention_name = str(retention.get("name") or "")
    sampler_name = str(sampler.get("name") or "")
    if not retention_name and not sampler_name:
        return '<p class="empty">Population policy provenance unavailable.</p>'

    retention_options = _mapping(retention.get("options"))
    objectives = tuple(str(item) for item in _sequence(retention_options.get("objectives")))
    if retention_name == "metric_pareto":
        effective_mode = (
            "Multi-objective nondominated fronts"
            if len(objectives) >= 2
            else "Scalar fallback (one objective)"
        )
    else:
        effective_mode = "Scalar archive ordering"
    objective_text = ", ".join(objectives) if objectives else "default"
    epsilon = retention_options.get("epsilon")
    epsilon_text = str(epsilon) if epsilon is not None else "default"
    sampler_options = _mapping(sampler.get("options"))
    sampler_detail = ", ".join(f"{key}={value}" for key, value in sorted(sampler_options.items()))
    sampler_text = (
        f"{sampler_name} ({sampler_detail})" if sampler_detail else sampler_name or "unavailable"
    )
    return (
        '<div class="run-summary population-summary">'
        f'<div class="metric"><span>Retention</span><strong><code>{_h(retention_name or "unavailable")}'
        "</code></strong></div>"
        f'<div class="metric"><span>Objectives</span><strong><code>{_h(objective_text)}'
        f"</code></strong><span>epsilon={_h(epsilon_text)}</span></div>"
        f'<div class="metric"><span>Effective mode</span><strong>{_h(effective_mode)}'
        "</strong></div>"
        f'<div class="metric"><span>Parent sampler</span><strong><code>{_h(sampler_text)}'
        "</code></strong></div>"
        "</div>"
    )


def _finite_score(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if math.isfinite(score) else None


def _hidden_scores(run_dir: Path) -> dict[str, float]:
    """Read final holdout scores without making the dashboard a state store."""

    payload = read_json(run_dir / "final_ranking.json")
    scores: dict[str, float] = {}
    for value in _sequence(payload):
        row = _mapping(value)
        candidate_id = row.get("candidate_id")
        score = _finite_score(row.get("final_score"))
        if isinstance(candidate_id, str) and score is not None:
            scores[candidate_id] = score
    if scores:
        return scores

    selection = _mapping(read_json(run_dir / "validation_selection.json"))
    champion = selection.get("champion")
    if isinstance(champion, str):
        result = _mapping(
            read_json(run_dir / "controller" / "final_evaluations" / champion / "result.json")
        )
        score = _finite_score(result.get("score"))
        if score is not None and result.get("success", True) is not False:
            scores[champion] = score
    return scores


def _attempt_validation_score(
    private: Mapping[str, Any], run_dir: Path, attempt_id: str
) -> float | None:
    """Return validation as soon as controller-only evidence is available."""

    if private.get("valid") is True:
        score = _finite_score(private.get("validation_score"))
        if score is not None:
            return score
    result = _mapping(
        read_json(
            run_dir / "controller" / "evaluations" / attempt_id / "validation" / "result.json"
        )
    )
    score = _finite_score(result.get("score"))
    if score is not None and result.get("success", True) is not False:
        return score
    return None


def _attempt_outcome(attempt: Mapping[str, Any], private: Mapping[str, Any]) -> str:
    status = _safe_status(attempt.get("status"))
    if status in {"pending", "running"}:
        return "running"
    if private.get("gate") == "quarantined":
        return "quarantined"
    if private.get("valid") is True:
        return "retained" if private.get("accepted") is True else "not-retained"
    return "rejected"


def _score_points(
    state: Mapping[str, Any], details: Mapping[str, Any], run_dir: Path
) -> list[dict[str, Any]]:
    private_attempts = _mapping(details.get("attempts"))
    hidden = _hidden_scores(run_dir)
    points: list[dict[str, Any]] = []
    for source_order, value in enumerate(_sequence(state.get("attempts")), start=1):
        attempt = _mapping(value)
        attempt_id = str(attempt.get("attempt_id"))
        private = _mapping(private_attempts.get(attempt_id))
        validation = _attempt_validation_score(private, run_dir, attempt_id)
        observed_order = private.get("public_observed_order")
        if not isinstance(observed_order, int):
            observed_order = private.get("validation_observed_order")
        points.append(
            {
                "source_order": source_order,
                "observed_order": observed_order,
                "attempt_id": attempt_id,
                "iteration": attempt.get("iteration"),
                "island": attempt.get("island"),
                "parent_id": private.get("parent_id") or attempt.get("parent_id"),
                "guidance": private.get("guidance") or attempt.get("guidance"),
                "outcome": _attempt_outcome(attempt, private),
                "public": _attempt_public_score(attempt, private),
                "validation": validation,
                "hidden": hidden.get(attempt_id),
            }
        )
    points.sort(
        key=lambda point: (
            not isinstance(point.get("observed_order"), int),
            point.get("observed_order")
            if isinstance(point.get("observed_order"), int)
            else point["source_order"],
            point["source_order"],
        )
    )
    for order, point in enumerate(points, start=1):
        point["order"] = order
    return points


def _score_number(value: object, digits: int = 4) -> str:
    score = _finite_score(value)
    return f"{score:.{digits}f}" if score is not None else "-"


def _best_observed(
    points: list[dict[str, Any]], metric: str
) -> tuple[dict[str, Any], float] | None:
    scored = [
        (point, score)
        for point in points
        if (score := _finite_score(point.get(metric))) is not None
    ]
    return max(scored, key=lambda item: item[1]) if scored else None


def _ordered_values(points: list[dict[str, Any]], key: str) -> list[object]:
    values: list[object] = []
    for point in points:
        value = point.get(key)
        if value not in values:
            values.append(value)
    return values


def _ordered_islands(points: list[dict[str, Any]]) -> list[object]:
    """Return island identifiers in numeric order when they are numeric."""

    def sort_key(value: object) -> tuple[int, int, str]:
        if isinstance(value, int) and not isinstance(value, bool):
            return (0, value, "")
        text = str(value)
        if re.fullmatch(r"[+-]?\d+", text):
            return (0, int(text), text)
        if value is None:
            return (2, 0, "")
        return (1, 0, text.casefold())

    return sorted(_ordered_values(points, "island"), key=sort_key)


def _island_view_id(prefix: str, island: object) -> str:
    if island is None:
        token = "none"
    else:
        token = quote(str(island), safe="") or "empty"
    return f"{prefix}-{token}"


def _metric_point_svg(point: Mapping[str, Any], x: float, y: float, metric: str) -> str:
    attempt_id = str(point["attempt_id"])
    outcome = str(point["outcome"])
    score = float(point[metric])
    title = (
        f"completion {point.get('display_order')} · {attempt_id} · island {point.get('island')} · "
        f"iteration {point.get('iteration')} · {metric} {score:.6f} · {outcome}"
    )
    return (
        f'<a href="{_href("owner/attempts", attempt_id)}"><title>{_h(title)}</title>'
        f'<circle class="metric-point {_h(metric)} {_h(outcome)}" '
        f'cx="{x:.1f}" cy="{y:.1f}" r="4.5"/></a>'
    )


def _render_score_chart(
    points: list[dict[str, Any]], view_label: str, y_min: float, y_max: float
) -> str:
    display = [
        point
        for point in points
        if any(
            _finite_score(point.get(metric)) is not None
            for metric in ("public", "validation", "hidden")
        )
    ]
    if not display:
        return '<p class="empty">No candidate score has been recorded for this view.</p>'
    for order, point in enumerate(display, start=1):
        point["display_order"] = order

    width = max(920, 120 + 42 * max(len(display) - 1, 1))
    height = 330
    left, right, top, bottom = 62.0, 24.0, 24.0, 66.0
    plot_width = width - left - right
    plot_height = height - top - bottom

    def x_at(index: int) -> float:
        if len(display) == 1:
            return left + plot_width / 2
        # Keep early runs readable: two observations should not be stretched
        # across an ultrawide dashboard. The series expands naturally as the
        # run accumulates candidates, then uses the full plot width.
        series_width = min(plot_width, 84.0 * (len(display) - 1))
        series_left = left + (plot_width - series_width) / 2
        return series_left + index * series_width / (len(display) - 1)

    def y_at(score: float) -> float:
        return top + (y_max - score) * plot_height / max(y_max - y_min, 1e-12)

    coordinates: dict[tuple[str, int], tuple[float, float]] = {}
    for index, point in enumerate(display):
        for metric in ("public", "validation", "hidden"):
            score = _finite_score(point.get(metric))
            if score is not None:
                coordinates[(metric, int(point["order"]))] = (x_at(index), y_at(score))

    svg: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="Candidate scores in completion order for {_h(view_label)}">'
    ]
    for tick in range(5):
        value = y_min + (y_max - y_min) * tick / 4
        y = y_at(value)
        svg.append(
            f'<line class="grid" x1="{left:.1f}" y1="{y:.1f}" '
            f'x2="{width - right:.1f}" y2="{y:.1f}"/>'
            f'<text class="axis-label" x="{left - 10:.1f}" y="{y + 4:.1f}" '
            f'text-anchor="end">{value:.2f}</text>'
        )
    if y_min <= 0.5 <= y_max:
        tie_y = y_at(0.5)
        svg.append(
            f'<line class="tie-line" x1="{left:.1f}" y1="{tie_y:.1f}" '
            f'x2="{width - right:.1f}" y2="{tie_y:.1f}"/>'
            f'<text class="axis-label" x="{width - right:.1f}" y="{tie_y - 6:.1f}" '
            'text-anchor="end">reference tie</text>'
        )
    label_step = max(1, math.ceil(len(display) / 10))
    for index, point in enumerate(display):
        if index % label_step and index != len(display) - 1:
            continue
        x = x_at(index)
        svg.append(
            f'<line class="grid" x1="{x:.1f}" y1="{top:.1f}" '
            f'x2="{x:.1f}" y2="{height - bottom + 6:.1f}"/>'
            f'<text class="iteration-label" x="{x:.1f}" y="{height - 27:.1f}" '
            f'text-anchor="middle">#{index + 1}</text>'
            f'<text class="axis-label" x="{x:.1f}" y="{height - 12:.1f}" '
            f'text-anchor="middle">i{_h(point.get("iteration"))}</text>'
        )

    for metric in ("public", "validation", "hidden"):
        observations = [point for point in display if _finite_score(point.get(metric)) is not None]
        if len(observations) >= 2:
            values = " ".join(
                f"{coordinates[(metric, int(point['order']))][0]:.1f},"
                f"{coordinates[(metric, int(point['order']))][1]:.1f}"
                for point in observations
            )
            svg.append(f'<polyline class="metric-line {_h(metric)}" points="{values}"/>')
        for point in observations:
            x, y = coordinates[(metric, int(point["order"]))]
            svg.append(_metric_point_svg(point, x, y, metric))
    svg.append("</svg>")
    return (
        '<div class="score-chart-head"><strong>Candidate score history</strong>'
        f"<span>Completion order · {_h(view_label)}</span></div>"
        '<div class="score-chart">' + "".join(svg) + "</div>"
    )


def _render_score_timeline(
    state: Mapping[str, Any], details: Mapping[str, Any], run_dir: Path
) -> str:
    """Render owner-only candidate metrics from authoritative run evidence."""

    points = _score_points(state, details, run_dir)
    scored = [
        score
        for point in points
        for metric in ("public", "validation", "hidden")
        if (score := _finite_score(point.get(metric))) is not None
    ]
    if not points:
        return '<p class="empty">No candidate attempts recorded yet.</p>'

    best_public = _best_observed(points, "public")
    best_validation = _best_observed(points, "validation")
    best_hidden = _best_observed(points, "hidden")

    def best_card(label: str, item: tuple[dict[str, Any], float] | None) -> str:
        if item is None:
            return (
                f'<div class="metric"><span>{_h(label)}</span>'
                '<strong class="missing">Pending</strong></div>'
            )
        point, score = item
        attempt_id = str(point["attempt_id"])
        return (
            f'<div class="metric"><span>{_h(label)}</span>'
            f'<strong>{score:.4f}</strong><span><a href="{_href("owner/attempts", attempt_id)}">'
            f"<code>{_h(attempt_id)}</code></a> · {_h(point['outcome'])}</span></div>"
        )

    evaluated = sum(
        1
        for point in points
        if any(
            _finite_score(point.get(metric)) is not None
            for metric in ("public", "validation", "hidden")
        )
    )
    summary = (
        '<div class="run-summary score-summary">'
        f'<div class="metric"><span>Evaluated attempts</span><strong>{evaluated} / {len(points)}</strong></div>'
        + best_card("Best observed public", best_public)
        + best_card("Best observed validation", best_validation)
        + best_card("Hidden champion", best_hidden)
        + "</div>"
    )
    if not scored:
        return summary + '<p class="empty">No evaluation score has been recorded yet.</p>'

    minimum, maximum = min(scored), max(scored)
    normalized = minimum >= 0.0 and maximum <= 1.0
    if normalized:
        y_min, y_max = 0.0, 1.0
    else:
        padding = max((maximum - minimum) * 0.08, 1e-6)
        y_min, y_max = minimum - padding, maximum + padding

    islands = _ordered_islands(points)
    legend = (
        '<div class="score-legend">'
        '<span><i class="score-swatch public"></i>Public</span>'
        '<span><i class="score-swatch validation"></i>Validation (owner only)</span>'
        '<span><i class="score-swatch hidden"></i>Hidden champion (owner only)</span>'
        "</div>"
    )
    options = ['<option value="all">All islands</option>']
    views = [
        '<div class="score-view" data-score-view="all">'
        + _render_score_chart(points, "All islands", y_min, y_max)
        + "</div>"
    ]
    for island in islands:
        view_id = _island_view_id("island", island)
        label = f"Island {island if island is not None else '-'}"
        options.append(f'<option value="{view_id}">{_h(label)}</option>')
        island_points = [point for point in points if point.get("island") == island]
        views.append(
            f'<div class="score-view" data-score-view="{view_id}" hidden>'
            + _render_score_chart(island_points, label, y_min, y_max)
            + "</div>"
        )
    toolbar = (
        '<div class="score-toolbar"><label for="score-view-filter">View</label>'
        '<select id="score-view-filter">' + "".join(options) + "</select></div>"
    )
    filter_script = """<script>
(function () {
  "use strict";
  var select = document.getElementById("score-view-filter");
  if (!select) { return; }
  var views = document.querySelectorAll("[data-score-view]");
  var storageKey = "optiprofiler-evolve:" + window.location.pathname + ":score-view";
  try {
    var saved = window.sessionStorage.getItem(storageKey);
    if (saved && Array.prototype.some.call(select.options, function (option) {
      return option.value === saved;
    })) {
      select.value = saved;
    }
  } catch (error) {
    // Storage can be disabled without affecting the dashboard itself.
  }
  function apply() {
    Array.prototype.forEach.call(views, function (view) {
      view.hidden = view.getAttribute("data-score-view") !== select.value;
    });
    try { window.sessionStorage.setItem(storageKey, select.value); } catch (error) {}
  }
  select.addEventListener("change", apply);
  apply();
})();
</script>"""
    rows = []
    for point in points:
        attempt_id = str(point["attempt_id"])
        outcome = str(point["outcome"])
        rows.append(
            "<tr>"
            f"<td>{point['order']}</td>"
            f'<td><a href="{_href("owner/attempts", attempt_id)}"><code>{_h(attempt_id)}</code></a></td>'
            f"<td>{_h(point.get('iteration'))}</td><td>{_h(point.get('island'))}</td>"
            f'<td><span class="score-outcome {_h(outcome)}">{_h(outcome)}</span></td>'
            f"<td>{_score_number(point.get('public'), 6)}</td>"
            f"<td>{_score_number(point.get('validation'), 6)}</td>"
            f"<td>{_score_number(point.get('hidden'), 6)}</td>"
            "</tr>"
        )
    table = (
        '<details class="iter-group"><summary>Exact candidate metrics'
        f'<span class="count">{len(points)} attempts</span></summary>'
        '<div class="table-wrap"><table><thead><tr><th>#</th><th>Candidate</th>'
        "<th>Iteration</th><th>Island</th><th>Outcome</th><th>Public</th>"
        "<th>Validation</th><th>Hidden</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></details>"
    )
    note = (
        '<p class="subhead">Candidates are ordered by the first score recorded, not by island. '
        "Hover a point for candidate, island, iteration, outcome, and exact score. Missing "
        "stages remain absent rather than becoming zero. Hidden remains a one-shot champion "
        "evaluation, so it normally appears as one final point.</p>"
    )
    return summary + note + legend + toolbar + "".join(views) + filter_script + table


def _lineage_edge_label(point: Mapping[str, Any], parent: Mapping[str, Any] | None) -> str:
    bits: list[str] = []
    if parent is not None:
        child_score = _finite_score(point.get("public"))
        parent_score = _finite_score(parent.get("public"))
        if child_score is not None and parent_score is not None:
            bits.append(f"dP {child_score - parent_score:+.3f}")
    guidance = point.get("guidance")
    if isinstance(guidance, str) and guidance:
        bits.append(compact_text(guidance)[:18])
    return " · ".join(bits)


def _lineage_lanes(points: list[dict[str, Any]]) -> dict[str, float]:
    """Assign vertical tree lanes so siblings do not collapse onto one line."""

    by_id = {str(point["attempt_id"]): point for point in points}
    rank = {str(point["attempt_id"]): index for index, point in enumerate(points)}
    children: dict[str, list[str]] = {attempt_id: [] for attempt_id in by_id}
    roots: list[str] = []
    for point in points:
        attempt_id = str(point["attempt_id"])
        parent_id = str(point.get("parent_id") or "")
        if parent_id in by_id and parent_id != attempt_id:
            children[parent_id].append(attempt_id)
        else:
            roots.append(attempt_id)
    for child_ids in children.values():
        child_ids.sort(key=rank.__getitem__)
    roots.sort(key=rank.__getitem__)

    lanes: dict[str, float] = {}
    visiting: set[str] = set()
    next_lane = 0

    def place(attempt_id: str) -> float:
        nonlocal next_lane
        if attempt_id in lanes:
            return lanes[attempt_id]
        if attempt_id in visiting:
            lane = float(next_lane)
            next_lane += 1
            lanes[attempt_id] = lane
            return lane
        visiting.add(attempt_id)
        child_lanes = [place(child_id) for child_id in children[attempt_id]]
        visiting.discard(attempt_id)
        if child_lanes:
            lane = (min(child_lanes) + max(child_lanes)) / 2
        else:
            lane = float(next_lane)
            next_lane += 1
        lanes.setdefault(attempt_id, lane)
        return lanes[attempt_id]

    for attempt_id in roots:
        place(attempt_id)
    # Malformed cyclic or orphaned input should still render instead of hiding nodes.
    for attempt_id in by_id:
        place(attempt_id)
    return lanes


def _render_lineage_graph(
    island_points: list[dict[str, Any]],
    all_points: Mapping[str, Mapping[str, Any]],
    view_label: str,
    *,
    group_by_island: bool = False,
) -> str:
    ordered = sorted(
        island_points,
        key=lambda point: (
            point.get("iteration") is None,
            point.get("iteration") or 0,
            point["source_order"],
        ),
    )
    if not ordered:
        return '<p class="empty">No candidate lineage is available for this island.</p>'
    iterations = _ordered_values(ordered, "iteration")
    seed_x, first_x, column_gap, right = 112.0, 260.0, 190.0, 54.0
    width = max(920, first_x + column_gap * max(len(iterations) - 1, 0) + right)
    top, bottom = 48.0, 36.0

    def x_at(iteration: object) -> float:
        return first_x + iterations.index(iteration) * column_gap

    def iteration_slots(points: list[dict[str, Any]]) -> tuple[dict[str, float], int]:
        """Keep columns stable and only stack attempts that share an iteration."""

        slots: dict[str, float] = {}
        max_count = 1
        for iteration in iterations:
            members = [point for point in points if point.get("iteration") == iteration]
            members.sort(key=lambda point: point["source_order"])
            max_count = max(max_count, len(members))
            for index, point in enumerate(members):
                slots[str(point["attempt_id"])] = index - (len(members) - 1) / 2
        return slots, max_count

    band_rects: list[str] = []
    seed_marks: list[str] = []
    coordinates: dict[str, tuple[float, float]] = {}
    seed_coordinates: dict[object, tuple[float, float]] = {}
    if group_by_island:
        cursor = top
        band_gap = 14.0
        for island in _ordered_islands(ordered):
            members = [point for point in ordered if point.get("island") == island]
            local_slots, slot_count = iteration_slots(members)
            band_height = max(116.0, 84.0 + 44.0 * max(slot_count - 1, 0))
            center_y = cursor + band_height / 2
            for point in members:
                y = center_y + local_slots[str(point["attempt_id"])] * 44.0
                coordinates[str(point["attempt_id"])] = (x_at(point.get("iteration")), y)
            seed_coordinates[island] = (seed_x, center_y)
            island_label = f"Island {island if island is not None else '-'}"
            band_rects.append(
                f'<rect class="lineage-island-band" x="12" y="{cursor:.1f}" '
                f'width="{width - 24}" height="{band_height:.1f}" rx="6"/>'
                f'<text class="lineage-island-label" x="26" y="{cursor + 22:.1f}">'
                f"{_h(island_label)}</text>"
            )
            seed_marks.append(
                f'<line class="lineage-seed-rail" x1="{seed_x + 8:.1f}" y1="{center_y:.1f}" '
                f'x2="{width - right:.1f}" y2="{center_y:.1f}"/>'
                f'<circle class="lineage-seed-node" cx="{seed_x:.1f}" cy="{center_y:.1f}" r="6"/>'
                f'<text class="lineage-seed-label" x="{seed_x:.1f}" y="{center_y - 12:.1f}" '
                'text-anchor="middle">seed</text>'
            )
            cursor += band_height + band_gap
        height = max(250, math.ceil(cursor - band_gap + bottom))
    else:
        slots, slot_count = iteration_slots(ordered)
        height = max(250, int(152 + 48 * max(slot_count - 1, 0)))
        center_y = top + (height - top - bottom) / 2
        coordinates = {
            str(point["attempt_id"]): (
                x_at(point.get("iteration")),
                center_y + slots[str(point["attempt_id"])] * 48.0,
            )
            for point in ordered
        }
        island = ordered[0].get("island")
        seed_coordinates[island] = (seed_x, center_y)
        seed_marks.append(
            f'<line class="lineage-seed-rail" x1="{seed_x + 8:.1f}" y1="{center_y:.1f}" '
            f'x2="{width - right:.1f}" y2="{center_y:.1f}"/>'
            f'<circle class="lineage-seed-node" cx="{seed_x:.1f}" cy="{center_y:.1f}" r="6"/>'
            f'<text class="lineage-seed-label" x="{seed_x:.1f}" y="{center_y - 12:.1f}" '
            'text-anchor="middle">seed</text>'
        )

    svg = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="{_h(view_label)} candidate lineage">'
        '<defs><marker id="lineage-arrow" markerWidth="7" markerHeight="7" refX="6" '
        'refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" fill="var(--edge)"/>'
        '</marker><marker id="lineage-arrow-migration" markerWidth="7" markerHeight="7" '
        'refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" '
        'fill="var(--amber)"/></marker></defs>'
    ]
    svg.extend(band_rects)
    for iteration in iterations:
        x = x_at(iteration)
        svg.append(
            f'<line class="lineage-grid" x1="{x:.1f}" y1="34" x2="{x:.1f}" '
            f'y2="{height - bottom + 4:.1f}"/>'
            f'<text class="iteration-label" x="{x:.1f}" y="24" text-anchor="middle">'
            f"iteration {_h(iteration if iteration is not None else '-')}</text>"
        )
    svg.extend(seed_marks)
    for point in ordered:
        child_id = str(point["attempt_id"])
        child_x, child_y = coordinates[child_id]
        parent_id = point.get("parent_id")
        parent = all_points.get(str(parent_id)) if parent_id is not None else None
        edge_label = _lineage_edge_label(point, parent)
        if isinstance(parent_id, str) and parent_id in coordinates:
            parent_x, parent_y = coordinates[parent_id]
            midpoint_x = (parent_x + child_x) / 2
            migration = parent is not None and parent.get("island") != point.get("island")
            edge_class = "lineage-edge migration" if migration else "lineage-edge"
            marker = "lineage-arrow-migration" if migration else "lineage-arrow"
            display_label = edge_label
            if migration and parent is not None:
                migration_label = f"I{parent.get('island')} to I{point.get('island')}"
                display_label = (
                    f"{migration_label} · {edge_label}" if edge_label else migration_label
                )
            svg.append(
                f'<path class="{edge_class}" marker-end="url(#{marker})" '
                f'd="M {parent_x + 8:.1f} {parent_y:.1f} C {midpoint_x:.1f} {parent_y:.1f}, '
                f'{midpoint_x:.1f} {child_y:.1f}, {child_x - 9:.1f} {child_y:.1f}">'
                f"<title>{_h(parent_id)} to {_h(child_id)} · {_h(display_label or 'inherit')}</title>"
                "</path>"
            )
            if display_label:
                svg.append(
                    f'<text class="lineage-edge-label" x="{(parent_x + child_x) / 2:.1f}" '
                    f'y="{(parent_y + child_y) / 2 - 7:.1f}" text-anchor="middle">'
                    f"{_h(display_label)}</text>"
                )
        elif isinstance(parent_id, str) and parent_id and parent_id != "seed":
            start_x, start_y = seed_coordinates.get(
                point.get("island"), (seed_x, child_y)
            )
            external = compact_text(parent_id)[:15]
            migration = parent is not None and parent.get("island") != point.get("island")
            edge_class = "lineage-edge migration" if migration else "lineage-edge external"
            marker = "lineage-arrow-migration" if migration else "lineage-arrow"
            svg.append(
                f'<path class="{edge_class}" marker-end="url(#{marker})" '
                f'd="M {start_x + 8:.1f} {start_y:.1f} H {(start_x + child_x) / 2:.1f} '
                f'V {child_y:.1f} H {child_x - 9:.1f}">'
                f"<title>external parent {_h(parent_id)} to {_h(child_id)}</title></path>"
                f'<text class="lineage-edge-label" x="{start_x + 12:.1f}" y="{start_y - 6:.1f}">'
                f"{_h(external)}</text>"
            )
    for point in ordered:
        attempt_id = str(point["attempt_id"])
        x, y = coordinates[attempt_id]
        outcome = str(point["outcome"])
        title = (
            f"{attempt_id} · parent {point.get('parent_id') or '-'} · iteration {point.get('iteration')} · "
            f"public {_score_number(point.get('public'), 6)} · validation "
            f"{_score_number(point.get('validation'), 6)} · {outcome}"
        )
        svg.append(
            f'<a href="{_href("owner/attempts", attempt_id)}"><title>{_h(title)}</title>'
            f'<circle class="lineage-node {_h(outcome)}" cx="{x:.1f}" cy="{y:.1f}" r="7"/></a>'
            f'<text class="lineage-node-label" x="{x:.1f}" y="{y + 23:.1f}" '
            f'text-anchor="middle">{_h(attempt_id)}</text>'
        )
    svg.append("</svg>")
    return '<div class="lineage-chart">' + "".join(svg) + "</div>"


def _render_lineage(state: Mapping[str, Any], details: Mapping[str, Any], run_dir: Path) -> str:
    points = _score_points(state, details, run_dir)
    islands = _ordered_islands(points)
    if not islands:
        return '<p class="empty">No island lineage has been recorded yet.</p>'
    lookup = {str(point["attempt_id"]): point for point in points}
    options = ['<option value="all" selected>All islands</option>']
    views = [
        '<div class="lineage-view" data-lineage-view="all">'
        + _render_lineage_graph(points, lookup, "All islands", group_by_island=True)
        + "</div>"
    ]
    for island in islands:
        view_id = _island_view_id("lineage", island)
        label = f"Island {island if island is not None else '-'}"
        options.append(f'<option value="{view_id}">{_h(label)}</option>')
        island_points = [point for point in points if point.get("island") == island]
        views.append(
            f'<div class="lineage-view" data-lineage-view="{view_id}" hidden>'
            + _render_lineage_graph(island_points, lookup, label)
            + "</div>"
        )
    toolbar = (
        '<div class="score-toolbar"><label for="lineage-view-filter">Island</label>'
        '<select id="lineage-view-filter">' + "".join(options) + "</select></div>"
    )
    note = (
        '<p class="subhead">Columns are fixed iterations and rows are island swimlanes. Every node '
        "is an attempt; the faint seed rail means that the attempt still used the original seed. "
        "Arrows appear only for actual candidate inheritance, and amber dashed arrows show "
        "cross-island migration. Edge labels show public-score change and guidance when available.</p>"
    )
    legend = (
        '<div class="score-legend lineage-legend">'
        '<span><i class="lineage-key seed"></i>Original seed parent</span>'
        '<span><i class="lineage-key"></i>Within-island inheritance</span>'
        '<span><i class="lineage-key migration"></i>Cross-island migration</span>'
        "</div>"
    )
    script = """<script>
(function () {
  "use strict";
  var select = document.getElementById("lineage-view-filter");
  if (!select) { return; }
  var views = document.querySelectorAll("[data-lineage-view]");
  var storageKey = "optiprofiler-evolve:" + window.location.pathname + ":lineage-view-v2";
  try {
    var saved = window.sessionStorage.getItem(storageKey);
    if (saved && Array.prototype.some.call(select.options, function (option) {
      return option.value === saved;
    })) {
      select.value = saved;
    }
  } catch (error) {
    // Storage can be disabled without affecting the dashboard itself.
  }
  function apply() {
    Array.prototype.forEach.call(views, function (view) {
      view.hidden = view.getAttribute("data-lineage-view") !== select.value;
    });
    try { window.sessionStorage.setItem(storageKey, select.value); } catch (error) {}
  }
  select.addEventListener("change", apply);
  apply();
})();
</script>"""
    return note + legend + toolbar + "".join(views) + script


# One-line orientation for each built-in phase; unknown phases fall back to a
# generic line so custom components never break the page.
_PHASE_NOTES = {
    "prepare": (
        "Freezes the data split and manifest, snapshots the seed candidate and "
        "reference solver, and records the seed baselines."
    ),
    "explore": (
        "Island-parallel candidate attempts: worker mutation, static audit, smoke "
        "and public evaluation, mandatory integrity review, and population "
        "retention with after-iteration policies."
    ),
    "validate": (
        "Controller-only validation evaluations select the champion. Scores stay "
        "owner-side and are never fed back to workers."
    ),
    "hidden": (
        "One-shot hidden holdout evaluation of the fixed champion. Results are owner-only evidence."
    ),
    "report": "Writes the private final report and materializes the public bundle.",
    "direction_scout": (
        "Trusted research agent proposes direction cards later assigned to islands."
    ),
    "strategy_analysis": (
        "Per-island strategy attribution with executable leave-one-out ablations."
    ),
    "recombine": "Bounded cross-island recombination of analyzed strategies.",
    "challenger": "Post-selection strong-challenger comparison against the champion.",
}


def _attempt_public_score(attempt: Mapping[str, Any], private: Mapping[str, Any]) -> float | None:
    """The public score, or None when the ledger shows no evaluation evidence.

    New ledgers write null for unevaluated attempts, but historical ones carry
    a 0.0 placeholder. Authoritative evidence is a step that recorded a
    public_score metric (or a validated record, which implies evaluation);
    a 0.0 without either is the legacy placeholder, not a real score.
    """

    step_score = None
    for step in _mapping(private.get("steps")).values():
        candidate = _finite_score(_mapping(_mapping(step).get("metrics")).get("public_score"))
        if candidate is not None:
            step_score = candidate
            break
    score = _finite_score(attempt.get("public_score", private.get("public_score")))
    if score is None:
        score = step_score
    if score is None:
        return None
    evaluated = step_score is not None
    if score == 0.0 and not evaluated and private.get("valid") is not True:
        return None
    return score


def _attempt_display_status(status: str, private: Mapping[str, Any]) -> str:
    """Presentation status: quarantined is failure by review, shown as such."""

    if status == "failed" and _mapping(private).get("gate") == "quarantined":
        return "quarantined"
    return status


def _role_failure_class(private: Mapping[str, Any]) -> str | None:
    """Concise reason a trusted agent job failed, from its finished event."""

    missing = _sequence(private.get("missing_outputs"))
    if missing:
        return "missing outputs: " + ", ".join(str(name) for name in missing)
    if private.get("timed_out"):
        return "timeout"
    if private.get("cancelled"):
        return "cancelled"
    reason = private.get("termination_reason")
    if reason:
        return str(reason)
    returncode = private.get("returncode")
    if isinstance(returncode, int) and returncode != 0:
        return f"exit {returncode}"
    return None


_REVIEW_JOB_SUFFIX = re.compile(r"-r\d+$")


def _phase_role_values(state: Mapping[str, Any], phase_name: str) -> list[Any]:
    """Agent jobs belonging to a phase, including reviewer invocations.

    Reviewer jobs are emitted with their role name as the scope phase, so they
    are associated here through the attempt id their job id derives from.
    """

    attempt_ids = {
        str(_mapping(item).get("attempt_id"))
        for item in _sequence(state.get("attempts"))
        if str(_mapping(item).get("phase") or "") == phase_name
    }
    values = []
    for value in _sequence(state.get("roles")):
        role = _mapping(value)
        if str(role.get("phase") or "") == phase_name:
            values.append(value)
            continue
        base = _REVIEW_JOB_SUFFIX.sub("", str(role.get("job_id")))
        if base in attempt_ids:
            values.append(value)
    return values


def _phase_fallback_note(state: Mapping[str, Any], phase_name: str) -> str | None:
    """Fallback marker for a succeeded phase whose agent jobs failed."""

    failed = [
        value
        for value in _phase_role_values(state, phase_name)
        if _safe_status(_mapping(value).get("status")) == "failed"
    ]
    if not failed:
        return None
    jobs = "job" if len(failed) == 1 else "jobs"
    return f"fallback: {len(failed)} agent {jobs} failed"


def _matrix_summary(state: Mapping[str, Any]) -> dict[str, int]:
    islands: set[object] = set()
    iterations: set[object] = set()
    attempts = 0
    accepted = 0
    quarantined = 0
    for value in _sequence(state.get("matrix")):
        cell = _mapping(value)
        islands.add(cell.get("island"))
        iterations.add(cell.get("iteration"))
        counts = _mapping(cell.get("counts"))
        attempts += len(_sequence(cell.get("attempt_ids")))
        accepted += int(counts.get("accepted", 0) or 0)
        quarantined += int(counts.get("quarantined", 0) or 0)
    return {
        "islands": len(islands),
        "iterations": len(iterations),
        "attempts": attempts,
        "accepted": accepted,
        "quarantined": quarantined,
    }


def _render_owner_phase_graph(state: Mapping[str, Any], now: datetime | None) -> str:
    phases = _sequence(state.get("phases"))
    if not phases:
        return '<p class="empty">No phases recorded yet.</p>'
    summary = _matrix_summary(state)
    nodes = []
    for value in phases:
        phase = _mapping(value)
        name = str(phase.get("name", "phase"))
        status = _safe_status(phase.get("status"))
        sub = _label(status)
        duration = _duration_text(phase, now)
        if duration:
            sub += f" · {duration}"
        extra = ""
        fallback = _phase_fallback_note(state, name) if status == "succeeded" else None
        if fallback:
            extra += f'<span class="sub missing">{_h(fallback)}</span>'
        if (
            name == "explore"
            and status == "succeeded"
            and summary["attempts"]
            and not summary["accepted"]
        ):
            extra += '<span class="sub missing">completed with zero accepted candidates</span>'
        if name == "explore" and summary["attempts"]:
            extra += (
                '<span class="sub">'
                f"{summary['islands']} islands · {summary['iterations']} iterations · "
                f"{summary['attempts']} attempts · {summary['accepted']} accepted · "
                f"{summary['quarantined']} quarantined</span>"
            )
        body = (
            f"{_status_icon(status)}<div><strong>{_h(name)}</strong>"
            f'<span class="sub">{_h(sub)}</span>{extra}</div>'
        )
        page_name = safe_page_name(name)
        if page_name is not None:
            nodes.append(
                f'<a class="job-node" href="{_href("owner/phases", page_name)}">{body}</a>'
            )
        else:
            nodes.append(f'<span class="job-node">{body}</span>')
    controls = (
        '<div class="wf-controls">'
        '<button type="button" data-wf="fit">Fit</button>'
        '<button type="button" data-wf="out" aria-label="Zoom out">−</button>'
        '<button type="button" data-wf="reset">100%</button>'
        '<button type="button" data-wf="in" aria-label="Zoom in">+</button>'
        '<span class="wf-level" id="wf-level">100%</span></div>'
    )
    return (
        controls
        + '<div class="wf-canvas" id="wf-canvas"><div class="wf-inner" id="wf-inner">'
        + f'<div class="job-graph">{"".join(nodes)}</div></div></div>'
        + _WF_SCRIPT
    )


def _render_phase_page(
    phase: Mapping[str, Any],
    events: list[dict[str, Any]],
    state: Mapping[str, Any],
    details: Mapping[str, Any],
    run_dir: Path,
    now: datetime | None,
) -> str:
    name = str(phase.get("name", "phase"))
    status = _safe_status(phase.get("status"))
    refresh_head, refresh_script = _live_refresh(status)
    page_dir = run_dir / "owner" / "phases"
    phase_names = [str(_mapping(item).get("name")) for item in _sequence(state.get("phases"))]
    position = (
        f"Phase {phase_names.index(name) + 1} of {len(phase_names)}"
        if name in phase_names
        else "Phase"
    )
    parts = [_label(status)]
    duration = _duration_text(phase, now)
    if duration:
        parts.append(f"Duration {duration}")
    parts.append(position)
    fallback = _phase_fallback_note(state, name) if status == "succeeded" else None
    if fallback:
        parts.append(f"Fallback completion ({fallback})")
    if name == "explore" and status == "succeeded":
        summary = _matrix_summary(state)
        if summary["attempts"] and not summary["accepted"]:
            parts.append("Zero accepted candidates (admission or review failures)")
    subhead = " · ".join(_h(part) for part in parts)

    note = _PHASE_NOTES.get(name, "Configured workflow phase.")
    provenance = read_json(run_dir / "provenance.json")
    component = phase_component(provenance, name)
    options = _mapping(component.get("options"))
    if not options and name == "explore":
        resolved = _mapping(_mapping(provenance).get("config"))
        evolution = _mapping(resolved.get("evolution"))
        workers_config = _mapping(resolved.get("workers"))
        effective = {
            key: evolution.get(key)
            for key in (
                "islands",
                "iterations",
                "attempts_per_island",
                "population_per_island",
                "migration_interval",
            )
            if evolution.get(key) is not None
        }
        if workers_config.get("max_parallel") is not None:
            effective["workers.max_parallel"] = workers_config.get("max_parallel")
        options = effective
    if options:
        option_rows = "".join(
            f"<tr><td><code>{_h(key)}</code></td><td><code>{_h(compact_text(value))}"
            "</code></td></tr>"
            for key, value in sorted(options.items())
        )
        config_html = (
            f'<div class="table-wrap"><table><thead><tr><th>Option</th><th>Value</th>'
            f"</tr></thead><tbody>{option_rows}</tbody></table></div>"
        )
    elif component:
        config_html = "<p>No options configured; the phase runs with its defaults.</p>"
    else:
        config_html = '<p class="missing">Phase provenance unavailable.</p>'

    sections = [
        f'<section class="owner-section"><h2>About</h2><p>{_h(note)}</p>{config_html}</section>'
    ]

    if name == "explore":
        attempts_by_id = {
            str(_mapping(value).get("attempt_id")): _mapping(value)
            for value in _sequence(state.get("attempts"))
        }
        matrix = _render_matrix(state.get("matrix"), attempts_by_id, now)
        policy = _render_population_policy(run_dir)
        attempts = _render_owner_attempt_groups(
            state.get("attempts"), details, now, run_dir=run_dir, prefix="../attempts"
        )
        sections.append(f'<section class="owner-section"><h2>Island matrix</h2>{matrix}</section>')
        sections.append(
            f'<section class="owner-section"><h2>Population policy</h2>{policy}</section>'
        )
        sections.append(f'<section class="owner-section"><h2>Attempts</h2>{attempts}</section>')

    phase_roles = _phase_role_values(state, name)
    roles_html = _render_owner_roles(phase_roles, now, prefix="../roles", details=details)
    sections.append(
        f'<section class="owner-section"><h2>Agent jobs in this phase</h2>{roles_html}</section>'
    )

    artifact_items = [
        f"<li>{_relative_link(target, page_dir, run_dir)} — {_h(label)}</li>"
        for label, target in phase_artifacts(run_dir, name)
    ]
    artifacts_html = (
        f'<ul class="evidence-list">{"".join(artifact_items)}</ul>'
        if artifact_items
        else '<p class="missing">No phase evidence recorded yet.</p>'
    )
    sections.append(f'<section class="owner-section"><h2>Evidence</h2>{artifacts_html}</section>')
    job_ids = frozenset(str(_mapping(value).get("job_id")) for value in phase_roles)
    sections.append(
        '<section class="owner-section"><h2>Key events</h2>'
        f"{_render_phase_events(events, name, job_ids)}</section>"
    )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh_head}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Phase {_h(name)} — Owner Console</title>
  <style>{_STYLE}{_OWNER_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Owner console</span></header>
  {_BANNER}
  <main class="report" data-live-root>
    <p><a href="../../status.html">&larr; Owner console</a></p>
    <div class="run-head">{_status_icon(status)}<div><h1><code>{_h(name)}</code><span class="owner-tag">Private</span></h1><p class="subhead">{subhead}</p></div></div>
    {"".join(sections)}
    <p class="footnote">Private owner evidence page. Share only the public/ bundle.</p>
  </main>
  {refresh_script}
</body>
</html>
"""
    return document


def _render_phase_events(
    events: list[dict[str, Any]],
    phase_name: str,
    job_ids: frozenset[str] = frozenset(),
) -> str:
    rows = []
    for event in events:
        scope = _mapping(event.get("scope"))
        in_phase = str(scope.get("phase") or "") == phase_name
        in_jobs = str(scope.get("job_id") or "") in job_ids
        if (not in_phase and not in_jobs) or "attempt_id" in scope:
            continue
        context_bits = [
            f"{key} {scope[key]}"
            for key in ("iteration", "island", "job_id", "role", "step")
            if key in scope
        ]
        moment = _parse_ts(event.get("ts"))
        # A *_started row records that something began at that moment; its
        # ledger status ("running") is historical, not the current state, so
        # it renders as a static Started marker. Live spinners belong to the
        # lifecycle views (workflow nodes, attempt and job pages), which show
        # the aggregated current status.
        if str(event.get("kind", "")).endswith("_started"):
            status_html = _STARTED_BADGE
        else:
            status_html = _status_line(_safe_status(event.get("status")))
        rows.append(
            "<tr>"
            f"<td>{_h(event.get('seq'))}</td>"
            f"<td>{_h(moment.strftime('%H:%M:%S') if moment else '-')}</td>"
            f"<td><code>{_h(event.get('kind'))}</code></td>"
            f"<td>{status_html}</td>"
            f"<td>{_h(', '.join(context_bits) or '-')}</td>"
            "</tr>"
        )
    if not rows:
        return '<p class="missing">No phase-level events recorded yet.</p>'
    omitted = ""
    if len(rows) > PHASE_EVENT_LIMIT:
        omitted = (
            f'<p class="truncated">{len(rows) - PHASE_EVENT_LIMIT} earlier events '
            "omitted; the full ledger is events.jsonl.</p>"
        )
        rows = rows[-PHASE_EVENT_LIMIT:]
    return (
        '<div class="table-wrap phase-events"><table><thead><tr><th>Seq</th><th>Time</th><th>Event</th><th>Status</th><th>Context</th></tr></thead><tbody>'
        + "".join(rows)
        + f"</tbody></table></div>{omitted}"
    )


def _owner_run_head(
    run: Mapping[str, Any],
    run_status: str,
    state: Mapping[str, Any],
    now: datetime | None,
    run_dir: Path | None = None,
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
    known = {str(_mapping(item).get("attempt_id")) for item in _sequence(state.get("attempts"))}
    if isinstance(best, str) and best in known:
        best_html = f'<a href="{_href("owner/attempts", best)}"><code>{_h(best)}</code></a>'
    else:
        best_html = f"<code>{_h(best)}</code>"
    champion_links = []
    if run_dir is not None and isinstance(best, str):
        if (run_dir / "final_solver").is_dir():
            champion_links.append('<a href="final_solver/"><code>final_solver/</code></a>')
        candidate_tree = run_dir / "candidates" / best
        if candidate_tree.is_dir() and safe_page_name(best):
            champion_links.append(
                f'<a href="candidates/{_h(quote(best))}/"><code>candidates/{_h(best)}/</code></a>'
            )
    champion_html = (
        '<span class="sub">' + " · ".join(champion_links) + "</span>" if champion_links else ""
    )
    return (
        f'<div class="run-head">{_status_icon(run_status)}'
        f'<div><h1>Evolution run<span class="owner-tag">Private</span></h1>'
        f'<p class="subhead">{subhead}</p></div></div>'
        '<div class="run-summary">'
        f'<div class="metric"><span>Best candidate</span><strong>{best_html}'
        f"</strong>{champion_html}</div>"
        f'<div class="metric"><span>Iterations</span>'
        f"<strong>{len(_sequence(state.get('iterations')))}</strong></div>"
        f'<div class="metric"><span>Attempts</span>'
        f"<strong>{len(_sequence(state.get('attempts')))}</strong></div>"
        "</div>"
    )


def _render_owner_attempt_groups(
    values: object,
    details: Mapping[str, Any],
    now: datetime | None,
    *,
    run_dir: Path,
    prefix: str = "owner/attempts",
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
            private = _mapping(private_attempts.get(attempt_id))
            display = _attempt_display_status(status, private)
            score = _attempt_public_score(member, private)
            score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "-"
            validation = _attempt_validation_score(private, run_dir, attempt_id)
            validation_text = _score_number(validation)
            island = member.get("island")
            failure_note = ""
            if status == "failed" and private.get("error"):
                failure_note = (
                    f'<span class="missing">{_h(compact_text(str(private.get("error"))[:80]))}'
                    "</span>"
                )
            rows.append(
                f'<a class="attempt-link" href="{_href(prefix, attempt_id)}">'
                f"{_status_icon(display)}<code>{_h(attempt_id)}</code>"
                f'<span class="grow">Island {_h(island if island is not None else "-")} '
                f"{failure_note}</span>"
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


def _render_owner_roles(
    values: object,
    now: datetime | None,
    *,
    prefix: str = "owner/roles",
    details: Mapping[str, Any] | None = None,
) -> str:
    roles = _sequence(values)
    if not roles:
        return '<p class="empty">No trusted agent jobs recorded.</p>'
    private_roles = _mapping(_mapping(details or {}).get("roles"))
    rows = []
    for value in roles:
        role = _mapping(value)
        job_id = str(role.get("job_id"))
        status = _safe_status(role.get("status"))
        status_html = _status_line(status)
        if status == "failed":
            failure = _role_failure_class(_mapping(private_roles.get(job_id)))
            if failure:
                status_html += f'<div class="missing">{_h(failure)}</div>'
        rows.append(
            "<tr>"
            f'<td><a href="{_href(prefix, job_id)}"><code>{_h(job_id)}</code></a></td>'
            f"<td>{_h(role.get('role'))}</td>"
            f"<td>{_h(role.get('phase') or '-')}</td>"
            f"<td>{status_html}</td>"
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
    refresh_head, refresh_script = _live_refresh(status)
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
  {refresh_head}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Attempt {_h(attempt_id)} — Owner Console</title>
  <style>{_STYLE}{_OWNER_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Owner console</span></header>
  {_BANNER}
  <main class="report" data-live-root>
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
  {refresh_script}
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
    display = _attempt_display_status(status, private)
    parts = [_label(display)]
    duration = _duration_text(attempt, now)
    if duration:
        parts.append(f"Duration {duration}")
    island = attempt.get("island")
    iteration = attempt.get("iteration")
    parts.append(f"Iteration {iteration if iteration is not None else '-'}")
    parts.append(f"Island {island if island is not None else '-'}")
    subhead = " · ".join(_h(str(part)) for part in parts)
    parent_id = private.get("parent_id") or attempt.get("parent_id") or "-"
    known = {str(_mapping(item).get("attempt_id")) for item in _sequence(state.get("attempts"))}
    if isinstance(parent_id, str) and parent_id in known:
        parent_html = f'<a href="{_href(".", parent_id)}"><code>{_h(parent_id)}</code></a>'
    else:
        parent_html = f"<code>{_h(parent_id)}</code>"
    worker_name = private.get("worker_name")
    accepted = private.get("accepted")
    accepted_text = "Yes" if accepted is True else "No" if accepted is False else "-"
    return (
        f'<div class="run-head">{_status_icon(display)}'
        f"<div><h1><code>{_h(attempt_id)}</code>"
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
    public = _attempt_public_score(attempt, private)
    public_text = (
        f"{public:.6f}" if isinstance(public, (int, float)) else "unavailable (not evaluated)"
    )
    validation_dir = run_dir / "controller" / "evaluations" / attempt_id / "validation"
    validation = _attempt_validation_score(private, run_dir, attempt_id)
    if validation_dir.is_dir() and validation is not None:
        validation_html = (
            f"<strong>{_h(f'{validation:.6f}')}</strong> "
            f"{_link_or_missing(run_dir, validation_dir, page_dir, 'evaluation output')}"
        )
    else:
        validation_html = '<span class="missing">unavailable (not evaluated)</span>'
    hidden_dir = run_dir / "controller" / "final_evaluations" / attempt_id
    hidden = _hidden_scores(run_dir).get(attempt_id)
    if hidden_dir.is_dir():
        score_html = f"<strong>{hidden:.6f}</strong> " if hidden is not None else ""
        hidden_html = score_html + _link_or_missing(
            run_dir, hidden_dir, page_dir, "final evaluation output"
        )
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
    transcript = (
        resolve_recorded_path(str(worker.get("transcript") or ""), run_dir)
        or run_dir / "transcripts" / f"{attempt_id}.jsonl"
    )
    trace_dir = (
        resolve_recorded_path(str(worker.get("trace_chunks") or ""), run_dir)
        or run_dir / "traces" / attempt_id / "chunks.jsonl"
    ).parent
    facts = [
        ("Return code", worker.get("returncode", private.get("worker_returncode", "-"))),
        ("Timed out", worker.get("timed_out", private.get("worker_timed_out", "-"))),
        ("Cancelled", worker.get("cancelled", private.get("worker_cancelled", "-"))),
        (
            "Termination",
            worker.get("termination_reason", private.get("worker_termination_reason")) or "-",
        ),
    ]
    kv = "".join(
        f"<div><span>{_h(name)}</span><strong>{_h(value)}</strong></div>" for name, value in facts
    )
    capture_error = worker.get("trace_capture_error") or private.get("trace_capture_error")
    capture = (
        f'<p class="truncated">Trace capture degraded: <code>{_h(capture_error)}</code></p>'
        if capture_error
        else ""
    )
    transcript_html = _transcript_preview(transcript, run_dir, page_dir)
    streams_html = _stream_previews(trace_dir, run_dir, page_dir)
    links = [_link_or_missing(run_dir, trace_dir / name, page_dir, name) for name in _TRACE_FILES]
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
            sections.append(f'<h3>{_h(title)}</h3><p class="missing">{_h(name)} (unavailable)</p>')
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
                artifact_items.append(f'<span class="missing">{_h(artifact)} (unavailable)</span>')
        artifacts_html = (
            '<ul class="evidence-list">'
            + "".join(f"<li>{item}</li>" for item in artifact_items)
            + "</ul>"
            if artifact_items
            else ""
        )
        error = data.get("error")
        error_html = f"<p>Error: <code>{_h(error)}</code></p>" if error else ""
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
                '<p class="truncated">Diff truncated at generation limits.</p>' if truncated else ""
            )
            more = (
                f'<p class="truncated">Preview shows first {DIFF_PREVIEW_LINES} of '
                f"{len(lines)} lines.</p>"
                if len(lines) > DIFF_PREVIEW_LINES
                else ""
            )
            link = _relative_link(patch_path, page_dir, run_dir)
            diff_html = (
                f'<pre class="preview">{_h(preview)}</pre>{more}{note}<p>Full patch: {link}</p>'
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
        _link_or_missing(run_dir, broker_artifacts, page_dir, "worker-visible benchmark artifacts")
    ]
    if broker_artifacts.is_dir():
        for name in ("feedback.md", "artifact_index.json"):
            for found in sorted(broker_artifacts.rglob(name))[:4]:
                items.append(_relative_link(found, page_dir, run_dir))
    return '<ul class="evidence-list">' + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _attempt_gateway_section(private: Mapping[str, Any], run_dir: Path, page_dir: Path) -> str:
    worker = _mapping(private.get("worker"))
    outcome = worker.get("provider_gateway_outcome")
    count = worker.get("provider_gateway_request_count")
    manifest = resolve_recorded_path(str(worker.get("provider_gateway_manifest") or ""), run_dir)
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
    refresh_head, refresh_script = _live_refresh(status)
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
    failure = _role_failure_class(private) if status == "failed" else None
    facts = [
        ("Role", role_name or "-"),
        ("Return code", private.get("returncode", "-")),
        ("Timed out", private.get("timed_out", "-")),
        ("Termination", private.get("termination_reason") or "-"),
    ]
    if failure:
        facts.append(("Failure", failure))
    gateway_outcome = private.get("provider_gateway_outcome")
    if gateway_outcome:
        count = private.get("provider_gateway_request_count")
        gateway_text = str(gateway_outcome)
        if isinstance(count, int):
            gateway_text += f" ({count} requests)"
        facts.append(("Provider gateway", gateway_text))
    kv = "".join(
        f"<div><span>{_h(name)}</span><strong>{_h(value)}</strong></div>" for name, value in facts
    )
    workspace = run_dir / "research" / "roles" / role_name / job_id
    outputs = _sequence(private.get("outputs"))
    missing_outputs = _sequence(private.get("missing_outputs"))
    missing_note = ""
    if status == "failed" and missing_outputs:
        names = ", ".join(f"<code>{_h(name)}</code>" for name in missing_outputs)
        missing_note = (
            '<p class="truncated">Job failed: it finished its provider requests '
            f"but never wrote the declared outputs {names}.</p>"
        )
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
    links = [_link_or_missing(run_dir, trace_dir / name, page_dir, name) for name in _TRACE_FILES]
    links.append(_link_or_missing(run_dir, trace_dir, page_dir, "trace directory"))
    links.append(_link_or_missing(run_dir, workspace, page_dir, "role workspace"))
    transcript_html = _transcript_preview(transcript, run_dir, page_dir)
    streams_html = _stream_previews(trace_dir, run_dir, page_dir)
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh_head}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent job {_h(job_id)} — Owner Console</title>
  <style>{_STYLE}{_OWNER_STYLE}</style>
</head>
<body>
  <header class="topbar"><strong>OptiProfiler Evolve</strong><span>Owner console</span></header>
  {_BANNER}
  <main class="report" data-live-root>
    <p><a href="../../status.html">&larr; Owner console</a></p>
    <div class="run-head">{_status_icon(status)}<div><h1><code>{_h(job_id)}</code><span class="owner-tag">Private</span></h1><p class="subhead">{subhead}</p></div></div>
    <div class="kv">{kv}</div>
    <section class="owner-section"><h2>Transcript (messages and tool calls)</h2>{transcript_html}</section>
    <section class="owner-section"><h2>Raw capture</h2>{streams_html}<ul class="evidence-list">{"".join(f"<li>{item}</li>" for item in links)}</ul></section>
    <section class="owner-section"><h2>Declared outputs</h2>{missing_note}{outputs_html}</section>
    <p class="footnote">Private owner evidence page. Share only the public/ bundle.</p>
  </main>
  {refresh_script}
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
