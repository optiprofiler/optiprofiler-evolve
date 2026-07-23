from __future__ import annotations

import os
from pathlib import Path

from optiprofiler_evolve import evolve


ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = Path(os.environ.get("OPTIPROFILER_EVOLVE_RUN_DIR", "runs/github-actions"))

result = evolve(
    initial=ROOT / "examples" / "solver",
    interface="solver.py:solver",
    editable=["."],
    config=ROOT / "examples" / "experiment.yaml",
    run_dir=RUN_DIR,
)

print(result.run_dir)
print(result.best_solver)
print(result.public_score, result.final_score)
