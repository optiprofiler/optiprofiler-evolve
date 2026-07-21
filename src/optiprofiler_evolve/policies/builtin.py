"""Default read-only population policies."""

from __future__ import annotations

from ..protocols import IterationView, PopulationEdit


class MigrationPolicy:
    name = "migration"

    def __init__(self, *, interval: int = 2) -> None:
        if interval < 0:
            raise ValueError("Migration interval cannot be negative.")
        self.interval = interval

    def propose(self, view: IterationView) -> PopulationEdit:
        if not self.interval or view.iteration % self.interval:
            return PopulationEdit()
        champions = [
            (island, population[0])
            for island, population in enumerate(view.populations)
            if population
        ]
        if len(champions) < 2:
            return PopulationEdit()
        migrations = tuple(
            (champions[(index - 1) % len(champions)][1].candidate_id, target)
            for index, (target, _champion) in enumerate(champions)
        )
        return PopulationEdit(migrate=migrations)


class EarlyStopPolicy:
    name = "early_stop"

    def __init__(self, *, min_iteration: int = 1, score: float = 1.0) -> None:
        self.min_iteration = min_iteration
        self.score = score

    def propose(self, view: IterationView) -> PopulationEdit:
        best = max(
            (record.validation_score for population in view.populations for record in population),
            default=float("-inf"),
        )
        stop = view.iteration >= self.min_iteration and best >= self.score
        return PopulationEdit(stop=stop, reason="target score reached" if stop else None)


__all__: list[str] = []
