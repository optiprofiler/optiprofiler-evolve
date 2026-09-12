"""Static CSS and JavaScript assets for the private owner console pages."""


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
