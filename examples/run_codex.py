from pathlib import Path

from optiprofiler_evolve import evolve


HERE = Path(__file__).resolve().parent

result = evolve(
    initial=HERE / "solver",
    interface="solver.py:solver",
    editable=["."],
    config=HERE / "experiment-codex.yaml",
)

print(result.run_dir)
print(result.best_solver)
print(result.public_score, result.final_score)
