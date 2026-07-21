"""Server-free run status and final report rendering."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .events import read_events, write_run_state


def render_status(events_path: Path, destination: Path) -> None:
    """Atomically render an inline, GitHub-Actions-like status summary."""

    state = write_run_state(events_path, destination.parent / "run_state.json")
    phases = (
        "".join(
            f"<li><strong>{html.escape(name)}</strong> "
            f'<span class="{html.escape(str(status))}">{html.escape(str(status))}</span></li>'
            for name, status in state["phases"].items()
        )
        or "<li>No phases recorded yet.</li>"
    )
    matrix = (
        "".join(
            "<tr>"
            f"<td>{html.escape(key.split(':')[0])}</td>"
            f"<td>{html.escape(key.split(':')[1])}</td>"
            f"<td>{html.escape(str(value['status']))}</td>"
            f"<td>{html.escape(', '.join(value['attempts']))}</td>"
            "</tr>"
            for key, value in sorted(
                state["matrix"].items(),
                key=lambda item: tuple(int(part) for part in item[0].split(":")),
            )
        )
        or '<tr><td colspan="4">No attempts recorded yet.</td></tr>'
    )
    attempts = (
        "".join(
            "<tr>"
            f"<td>{html.escape(attempt_id)}</td>"
            f"<td>{html.escape(str(value['status']))}</td>"
            f"<td>{html.escape(_step_summary(value['steps']))}</td>"
            "</tr>"
            for attempt_id, value in sorted(state["attempts"].items())
        )
        or '<tr><td colspan="3">No attempts recorded yet.</td></tr>'
    )
    roles = (
        "".join(
            "<tr>"
            f"<td>{html.escape(job_id)}</td>"
            f"<td>{html.escape(str(value['role']))}</td>"
            f"<td>{html.escape(str(value['island'] if value['island'] is not None else '-'))}</td>"
            f"<td>{html.escape(str(value['status']))}</td>"
            "</tr>"
            for job_id, value in sorted(state["roles"].items())
        )
        or '<tr><td colspan="4">No research roles recorded.</td></tr>'
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptiProfiler Evolve Status</title>
  <style>
    body {{ margin: 0; font: 14px system-ui, sans-serif; color: #1f2328; background: #f6f8fa; }}
    main {{ max-width: 1120px; margin: 32px auto; padding: 0 20px; }}
    h1 {{ font-size: 24px; }} h2 {{ font-size: 16px; margin-top: 28px; }}
    .summary {{ display: flex; gap: 24px; padding: 16px; background: white; border: 1px solid #d0d7de; border-radius: 6px; }}
    ul {{ display: flex; gap: 8px; flex-wrap: wrap; padding: 0; list-style: none; }}
    li {{ padding: 10px 12px; background: white; border: 1px solid #d0d7de; border-radius: 6px; }}
    span {{ margin-left: 8px; }} .succeeded {{ color: #1a7f37; }} .failed {{ color: #cf222e; }} .running {{ color: #0969da; }}
    table {{ width: 100%; border-collapse: collapse; background: white; }} th, td {{ padding: 9px; border: 1px solid #d0d7de; text-align: left; }}
  </style>
</head>
<body><main>
  <h1>OptiProfiler Evolve</h1>
  <div class="summary"><div>Run: <strong>{html.escape(str(state["run"]))}</strong></div><div>Last event: {state["last_seq"]}</div></div>
  <h2>Phases</h2><ul>{phases}</ul>
  <h2>Island x iteration</h2>
  <table><thead><tr><th>Iteration</th><th>Island</th><th>Status</th><th>Attempts</th></tr></thead><tbody>{matrix}</tbody></table>
  <h2>Attempt pipeline</h2>
  <table><thead><tr><th>Attempt</th><th>Status</th><th>Steps</th></tr></thead><tbody>{attempts}</tbody></table>
  <h2>Research roles</h2>
  <table><thead><tr><th>Job</th><th>Role</th><th>Island</th><th>Status</th></tr></thead><tbody>{roles}</tbody></table>
</main></body></html>
"""
    _atomic_write(destination, document)


def render_final_report(events_path: Path, destination: Path) -> None:
    """Render a stable final report with links into the run artifact tree."""

    events = read_events(events_path)
    payload = html.escape(json.dumps(events[-20:], indent=2, default=str))
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OptiProfiler Evolve Report</title><style>body{{max-width:1000px;margin:32px auto;font:14px system-ui,sans-serif;padding:0 20px}}pre{{white-space:pre-wrap;background:#f6f8fa;padding:16px;border:1px solid #d0d7de}}</style></head>
<body><h1>Evolution report</h1><p><a href="status.html">Run status</a> · <a href="FINAL_REPORT.md">Markdown summary</a> · <a href="events.jsonl">Event ledger</a></p><h2>Latest events</h2><pre>{payload}</pre></body></html>
"""
    _atomic_write(destination, document)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _step_summary(steps: list[dict[str, object]]) -> str:
    ordered = sorted(
        steps,
        key=lambda item: (
            item["index"] is None,
            int(item["index"]) if item["index"] is not None else 0,
            str(item["name"]),
        ),
    )
    return " -> ".join(f"{step['name']}:{step['status']}" for step in ordered)


__all__: list[str] = []
