"""Private final reports derived from fixed controller results and run artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .models import FinalistResult
from .research import read_json_object, source_diff


def _code_locations(bindings: object) -> list[str]:
    locations = []
    if isinstance(bindings, list):
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            span = binding.get("lines", [])
            if isinstance(span, list) and len(span) == 2:
                locations.append(f"`{binding.get('file')}:{span[0]}-{span[1]}`")
    return locations


def write_final_report(
    run_dir: Path,
    best: FinalistResult,
    finalists: list[FinalistResult],
    final_solver: Path,
    explanation: Path,
) -> None:
    lines = [
        "# Evolution Result",
        "",
        f"- Best candidate: `{best.candidate_id}`",
        f"- Public fitness: `{best.public_score:.6f}`",
        f"- Validation fitness: `{best.validation_score:.6f}`",
        f"- Hidden fitness: `{best.final_score:.6f}`",
        f"- Materialized solver: `{final_solver}`",
        f"- Human-readable algorithm explanation: `{explanation}`",
        "",
        "## Validation-selected champion",
        "",
        "| Candidate | Island | Public | Validation | Hidden | Status |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in sorted(finalists, key=lambda value: -value.final_score):
        lines.append(
            f"| `{item.candidate_id}` | {item.island} | "
            f"{item.public_score:.6f} | {item.validation_score:.6f} | "
            f"{item.final_score:.6f} | "
            f"{'ok' if item.success else item.error or 'failed'} |"
        )
    research_artifacts = [
        path
        for path in (
            run_dir / "research" / "directions.json",
            run_dir / "research" / "analysis" / "island_bundles.json",
            run_dir / "research" / "strategy_transfer.json",
            run_dir / "research" / "recombination.json",
            run_dir / "research" / "challenger_report.json",
        )
        if path.is_file()
    ]
    if research_artifacts:
        lines.extend(["", "## Research workflow artifacts", ""])
        lines.extend(
            f"- [`{path.relative_to(run_dir).as_posix()}`]({path.relative_to(run_dir).as_posix()})"
            for path in research_artifacts
        )
    complexity_path = run_dir / "controller" / "complexity" / "final.json"
    if complexity_path.is_file():
        complexity = read_json_object(complexity_path)
        lines.extend([
            "", "## Source complexity", "",
            f"- Score: `{complexity['score']}` (initial: 100)",
            f"- Limit ratio: `{complexity['max_ratio']}`",
            f"- Mode/status: `{complexity['mode']}` / `{complexity['status']}`",
            "- [Detailed source diagnostics](controller/complexity/final.json)",
        ])
        candidate = complexity.get("candidate")
        if isinstance(candidate, Mapping):
            for item in candidate.get("hotspots", []):
                lines.append(f"- `{item['path']}:{item['line']}` {item['name']}: CC {item['cc']}")
    (run_dir / "FINAL_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_final_solver_explanation(
    run_dir: Path,
    best: FinalistResult,
    final_solver: Path,
) -> Path:
    source = run_dir / "explanations" / f"{best.candidate_id}.json"
    payload: Mapping[str, object] = {}
    if source.is_file():
        try:
            loaded = read_json_object(source)
            if loaded.get("status") == "complete":
                payload = loaded
        except ValueError:
            pass

    diff_path = run_dir / "final_solver.diff"
    diff_path.write_text(
        source_diff(run_dir / "candidates" / "seed", final_solver),
        encoding="utf-8",
    )
    lines = [
        "# Final Solver Explanation",
        "",
        f"- Candidate: `{best.candidate_id}`",
        f"- Island: `{best.island}`",
        "- Source diff: [`final_solver.diff`](final_solver.diff)",
        "",
    ]
    if payload:
        lines.extend(["## Summary", "", str(payload.get("summary", "")), ""])
        pseudocode = payload.get("pseudocode", [])
        if isinstance(pseudocode, list) and pseudocode:
            lines.extend(["## Pseudocode", ""])
            lines.extend(f"{index}. {step}" for index, step in enumerate(pseudocode, 1))
            lines.append("")
        mechanisms = payload.get("mechanisms", [])
        if isinstance(mechanisms, list) and mechanisms:
            lines.extend(["## Mechanisms", ""])
            for mechanism in mechanisms:
                if not isinstance(mechanism, Mapping):
                    continue
                lines.append(
                    f"- **{mechanism.get('name', 'Unnamed')}**: {mechanism.get('description', '')}"
                )
                locations = _code_locations(mechanism.get("code_bindings", []))
                if locations:
                    lines.append(f"  Code: {', '.join(locations)}")
            lines.append("")
        strengths = payload.get("expected_strengths", [])
        if isinstance(strengths, list) and strengths:
            lines.extend(["## Expected Strengths", ""])
            lines.extend(f"- {item}" for item in strengths)
            lines.append("")
        limitations = payload.get("known_limitations", [])
        if isinstance(limitations, list) and limitations:
            lines.extend(["## Known Limitations", ""])
            lines.extend(f"- {item}" for item in limitations)
            lines.append("")
        consulted = payload.get("evidence_consulted", [])
        if isinstance(consulted, list) and consulted:
            lines.extend(["## Evidence Consulted", ""])
            lines.extend(f"- `{item}`" for item in consulted)
            lines.append("")
    else:
        lines.extend(
            [
                "## Explanation Status",
                "",
                "The winning candidate has no complete worker-authored explanation. "
                "Review the source diff and verified strategy artifacts before reuse.",
                "",
            ]
        )

    winning_strategy_ids, transferred_strategy_ids = _winning_strategy_ids(
        run_dir, best
    )
    strategy_paths = sorted(
        (run_dir / "research" / "analysis").glob("island-*/strategy_cards.json")
    )
    supported: list[tuple[int, Mapping[str, object]]] = []
    for path in strategy_paths:
        try:
            strategy_payload = read_json_object(path)
            source_island = int(strategy_payload.get("island", -1))
            cards = strategy_payload.get("cards", [])
        except ValueError:
            continue
        if isinstance(cards, list):
            supported.extend(
                (source_island, card)
                for card in cards
                if isinstance(card, Mapping)
                and str(card.get("strategy_id")) in winning_strategy_ids
                and isinstance(card.get("ablation"), Mapping)
                and card["ablation"].get("conclusion") == "supported"
            )
    if supported:
        lines.extend(["## Strategy Evidence for This Winner", ""])
        for source_island, card in supported:
            effect = card.get("ablation", {}).get("mean_effect_when_present")
            suffix = f" (measured effect `{effect}`)" if effect is not None else ""
            transfer_note = (
                f"; transferred from island {source_island}, where its isolated effect was measured"
                if str(card.get("strategy_id")) in transferred_strategy_ids
                else "; measured on the winner's source island"
            )
            lines.append(
                f"- `{card.get('strategy_id')}`: {card.get('claim', '')}"
                f"{suffix}{transfer_note}."
            )
            mechanism = str(card.get("mechanism", "")).strip()
            if mechanism:
                lines.append(f"  Mechanism: {mechanism}")
            pseudocode = card.get("pseudocode", [])
            if isinstance(pseudocode, list) and pseudocode:
                lines.append(
                    "  Pseudocode: " + " -> ".join(str(step) for step in pseudocode)
                )
            locations = _code_locations(card.get("code_bindings", []))
            if locations:
                lines.append(f"  Code: {', '.join(locations)}")
        lines.append("")

    destination = run_dir / "FINAL_SOLVER_EXPLANATION.md"
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def _winning_strategy_ids(
    run_dir: Path,
    best: FinalistResult,
) -> tuple[set[str], set[str]]:
    """Return only strategies retained or applied on the winning lineage."""

    selected: set[str] = set()
    transferred: set[str] = set()
    bundles_path = run_dir / "research" / "analysis" / "island_bundles.json"
    if bundles_path.is_file():
        try:
            bundles = read_json_object(bundles_path).get("bundles", [])
        except ValueError:
            bundles = []
        if isinstance(bundles, list):
            for bundle in bundles:
                if (
                    not isinstance(bundle, Mapping)
                    or bundle.get("island") != best.island
                ):
                    continue
                kept = bundle.get("kept_strategy_ids", [])
                if isinstance(kept, list):
                    selected.update(str(item) for item in kept)

    transfer_path = run_dir / "research" / "strategy_transfer.json"
    if transfer_path.is_file():
        try:
            islands = read_json_object(transfer_path).get("islands", [])
        except ValueError:
            islands = []
        if isinstance(islands, list):
            for island in islands:
                if (
                    not isinstance(island, Mapping)
                    or island.get("island") != best.island
                    or island.get("winner_candidate_id") != best.candidate_id
                ):
                    continue
                attempts = island.get("attempts", [])
                if not isinstance(attempts, list):
                    continue
                for attempt in attempts:
                    if (
                        not isinstance(attempt, Mapping)
                        or attempt.get("variant_id") != best.candidate_id
                    ):
                        continue
                    manifest = attempt.get("manifest", {})
                    if not isinstance(manifest, Mapping):
                        continue
                    applied = manifest.get("applied_strategy_ids", [])
                    if isinstance(applied, list):
                        transferred.update(str(item) for item in applied)
    selected.update(transferred)
    return selected, transferred


def append_trace_coverage(run_dir: Path, coverage: Mapping[str, object]) -> None:
    report = run_dir / "FINAL_REPORT.md"
    if not report.is_file():
        return
    capture = coverage.get("capture_quality", {})
    outcomes = coverage.get("outcomes", {})
    gateway = coverage.get("provider_gateway", {})
    if not all(isinstance(value, Mapping) for value in (capture, outcomes, gateway)):
        raise ValueError("Trace coverage has an invalid shape.")
    lines = [
        "",
        "## Agent trace coverage",
        "",
        f"- Total invocations: `{coverage.get('total', 0)}`",
        f"- Complete captures: `{capture.get('complete', 0)}`",
        f"- Degraded captures: `{capture.get('degraded', 0)}`",
        f"- Interrupted captures: `{capture.get('interrupted', 0)}`",
        f"- Failed worker outcomes: `{outcomes.get('failed', 0)}`",
        f"- Timed-out worker outcomes: `{outcomes.get('timed_out', 0)}`",
        f"- Cancelled worker outcomes: `{outcomes.get('cancelled', 0)}`",
        f"- Gateway invocations: `{gateway.get('total', 0)}`",
        f"- Completed gateways: `{gateway.get('completed', 0)}`",
        f"- Failed gateways: `{gateway.get('failed', 0)}`",
        f"- Interrupted gateways: `{gateway.get('interrupted', 0)}`",
        "- Private index: `controller/trace_index.jsonl`",
        "- Private coverage details: `controller/trace_coverage.json`",
    ]
    with report.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
