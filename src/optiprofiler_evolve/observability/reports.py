"""Private final reports derived from fixed controller results and run artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from optiprofiler_evolve.files import read_json_object
from optiprofiler_evolve.models import FinalistResult
from optiprofiler_evolve.solver import source_diff, tree_hash


def _score(value: float | None) -> str:
    return format(value, ".6f") if value is not None else "not evaluated"


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
        f"- Public fitness: `{_score(best.public_score)}`",
        f"- Validation fitness: `{_score(best.validation_score)}`",
        f"- Hidden fitness: `{_score(best.final_score)}`",
        f"- Formal weighted score: `{_score(best.assessment_score)}`",
        f"- Final weighted score: `{_score(best.final_assessment_score)}`",
        f"- Final assessment status: `{best.assessment_status}`",
        f"- Materialized solver: `{final_solver}`",
        f"- Human-readable algorithm explanation: `{explanation}`",
        "",
        "## Validation-selected champion",
        "",
        "| Candidate | Island | Public | Validation | Hidden | Status |",
        "|---|---:|---:|---:|---:|---|",
    ]
    # Rank finalists by their controller-only final score; unscored (skipped)
    # finalists are listed last in caller order.
    for item in sorted(
        finalists,
        key=lambda value: (
            value.final_score is None,
            -value.final_score if value.final_score is not None else 0.0,
        ),
    ):
        lines.append(
            f"| `{item.candidate_id}` | {item.island} | "
            f"{_score(item.public_score)} | {_score(item.validation_score)} | "
            f"{_score(item.final_score)} | "
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
    lines.extend(_staged_quality(run_dir, best.candidate_id, tree_hash(final_solver)))
    (run_dir / "FINAL_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _delivery(run_dir: Path) -> Mapping[str, object]:
    try:
        return read_json_object(run_dir / "final_delivery.json")
    except (OSError, ValueError):
        return {}


def stage_assessment(run_dir: Path, candidate_id: str, stage: str) -> Mapping[str, object] | None:
    """Read the controller assessment a candidate or research variant received at one stage."""
    roots = (
        ("final_evaluations",) if stage == "hidden"
        else ("evaluations", "research_evaluations")
    )
    for root in roots:
        path = run_dir / "controller" / root / candidate_id
        path = path / "assessment.json" if stage == "hidden" else path / stage / "assessment.json"
        if path.is_file():
            try:
                return read_json_object(path)
            except ValueError:
                return None
    return None


def _staged_quality(run_dir: Path, candidate_id: str, delivered_hash: str) -> list[str]:
    """Keep the stage where a criterion ran apart from the stage that only scored."""

    stages = (
        ("validation", "Validation audit (champion selection stage)"),
        ("hidden", "Hidden performance (final stage; criteria disabled here are not re-run)"),
    )
    lines: list[str] = []
    reports = {stage: stage_assessment(run_dir, candidate_id, stage) for stage, _ in stages}
    if any(reports.values()):
        lines.extend(["", "## Quality assessment"])
    for stage, title in stages:
        assessment = reports[stage]
        if assessment is None:
            continue
        lines.extend(["", f"### {title}", "", f"- Assessed tree hash: `{assessment.get('tree_hash')}`",
                      "", "| Standard | Status | Score | Weight | Note |", "|---|---|---:|---:|---|"])
        for name, item in assessment["results"].items():
            weight = assessment["criteria"][name]["weight"]
            lines.append(f"| {name} | {item['status']} | {_score(item['score'])} | {weight} | "
                         f"{item.get('error') or ''} |")
    if not any(reports.values()):
        return lines
    review = (reports["validation"] or {}).get("results", {}).get("integrity") or {}
    reviewed_hash = (reports["validation"] or {}).get("tree_hash")
    lines.extend(["", "### Integrity review coverage of the delivered solver", "",
                  f"- Delivered solver tree hash: `{delivered_hash}`"])
    if review.get("status") == "succeeded" and reviewed_hash == delivered_hash:
        lines.append("- ESTABLISHED at the validation stage: the independently reviewed tree "
                     "is byte-identical to the delivered tree. The hidden stage did not re-run review.")
    else:
        reason = (
            f"validation integrity status is `{review.get('status', 'absent')}`"
            if review.get("status") != "succeeded"
            else f"reviewed tree `{reviewed_hash}` differs from the delivered tree"
        )
        lines.append(f"- NOT ESTABLISHED for the delivered tree: {reason}.")
    return lines


def write_final_solver_explanation(
    run_dir: Path,
    best: FinalistResult,
    final_solver: Path,
) -> Path:
    source = run_dir / "explanations" / f"{best.candidate_id}.json"
    payload: Mapping[str, object] = {}
    provenance = _delivery(run_dir).get("explanation_provenance")
    if not isinstance(provenance, Mapping):
        provenance = {"valid": False, "evidence": "the report stage recorded no explanation provenance"}
    if source.is_file() and provenance.get("valid") is True:
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
        lines.extend([
            "## Explanation Provenance", "",
            f"- Delivered solver tree hash: `{tree_hash(final_solver)}`",
            f"- Evidence level: {provenance.get('evidence')}.", "",
        ])
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
                f"- Delivered solver tree hash: `{tree_hash(final_solver)}`",
                f"- Reason: {provenance.get('evidence')}.",
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
