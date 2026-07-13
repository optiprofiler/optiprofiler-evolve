from pathlib import Path

from optiprofiler_evolve import evolve


HERE = Path(__file__).resolve().parent

result = evolve(
    initial=HERE / "repository_solver",
    interface="solver.py:solver",
    editable=["*.py"],
    config=HERE / "experiment.yaml",
)

print(result.run_dir)
print(result.best_solver)
print(result.public_score, result.final_score)
