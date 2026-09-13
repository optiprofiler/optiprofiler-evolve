from pathlib import Path

from optiprofiler_evolve import evolve

HERE = Path(__file__).resolve().parent

result = evolve(
    initial=HERE / "solver",
    interface="solver.py:solver",
    config=HERE / "experiment-codex-subscription.yaml",
)
print(result.run_dir)
print(result.best_solver)
print(result.run_dir / "FINAL_SOLVER_EXPLANATION.md")
