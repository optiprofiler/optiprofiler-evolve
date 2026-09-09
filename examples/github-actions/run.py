from __future__ import annotations

import os
from pathlib import Path

from optiprofiler_evolve import evolve

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = Path(os.environ.get("OPTIPROFILER_EVOLVE_RUN_DIR", "runs/github-actions"))
CONFIG = Path(
    os.environ.get(
        "OPTIPROFILER_EVOLVE_CONFIG",
        str(ROOT / "examples" / "experiment.yaml"),
    )
)
if not CONFIG.is_absolute():
    CONFIG = ROOT / CONFIG

result = evolve(
    initial=ROOT / "examples" / "solver",
    interface="solver.py:solver",
    editable=["."],
    config=CONFIG,
    run_dir=RUN_DIR,
)

print(result.run_dir)
print(result.best_solver)
print(result.public_score, result.final_score)
