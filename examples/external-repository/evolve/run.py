from __future__ import annotations

import os
from pathlib import Path

from optiprofiler_evolve import evolve


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(
    os.environ.get(
        "OPTIPROFILER_EVOLVE_RUN_DIR",
        ROOT / "runs" / "github-actions",
    )
)

result = evolve(
    initial=ROOT / "solver",
    interface="solver.py:solver",
    editable=["."],
    config=ROOT / "evolve" / "experiment.yaml",
    run_dir=RUN_DIR,
)

print(f"run_dir={result.run_dir}")
print(f"best_solver={result.best_solver}")
print(f"public_score={result.public_score}")
print(f"final_score={result.final_score}")
