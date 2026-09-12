"""Run two trusted metadata phases without model calls or solver evaluation."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from pathlib import Path

from optiprofiler_evolve import evolve
from optiprofiler_evolve.config import ComponentConfig, WorkflowConfig, load_config
from optiprofiler_evolve.files import write_json
from optiprofiler_evolve.phases.contracts import PhaseContext, PhaseResult


class SourceInventoryPhase:
    name = "source_inventory"
    requires = frozenset()
    provides = frozenset({"source_files"})

    def __init__(self, source: Path):
        self.source = source

    def run(self, context: PhaseContext) -> PhaseResult:
        names = tuple(sorted(str(path.relative_to(self.source))
                             for path in self.source.rglob("*.py") if path.is_file()))
        return PhaseResult(artifacts={"source_files": names})


class SourceSummaryPhase:
    name = "source_summary"
    requires = frozenset({"source_files"})
    provides = frozenset({"source_summary"})

    def run(self, context: PhaseContext) -> PhaseResult:
        names = context.artifacts["source_files"]
        output = write_json(context.run_dir / "source_summary.json", {
            "file_count": len(names),
            "files_by_directory": dict(Counter(str(Path(name).parent) for name in names)),
        })
        return PhaseResult(artifacts={"source_summary": output})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).parent / "repository_solver")
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_dir():
        parser.error("--source must be an existing directory")
    # Only metadata phases execute; these settings do not authorize untrusted solvers.
    config = load_config({
        "execution": {"backend": "unsafe_local"},
        "sandbox": {"backend": "unsafe_local"},
        "workers": {"pool": [{"harness": "codex", "model": "unused"}]},
        "assessment": {"criteria": {
            "performance": {"enabled": False},
            "integrity": {"enabled": False},
            "complexity": {"enabled": True},
        }},
    })
    config = replace(config, workflow=WorkflowConfig(
        phases=tuple(ComponentConfig.from_component(phase) for phase in (
            SourceInventoryPhase(source), SourceSummaryPhase(),
        )),
        attempt_steps=(),
    ))
    result = evolve(source, config=config, run_dir=args.run_dir)
    print(result.artifacts["source_summary"])


if __name__ == "__main__":
    main()
