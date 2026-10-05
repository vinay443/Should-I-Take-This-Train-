"""Simple delay predictors that any model has to beat.

    zero                 every train is on time
    train_station        the median delay seen before for this train at this station
    hour_weekday         the median delay seen before at this hour, weekday and direction

Each falls back to the next coarser one when it has no history for a key, and finally
to the overall median. They are fitted on one row per observation (the "cold" rows of
`sitt.models.features`), so an observation isn't counted once per earlier reading.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

import numpy as np

BASELINES = ("zero", "train_station", "hour_weekday")


@dataclass
class Baselines:
    overall: float = 0.0
    by_train_station: dict[tuple[str, str], float] = field(default_factory=dict)
    by_hour_weekday: dict[tuple[int, int, str], float] = field(default_factory=dict)

    @classmethod
    def fit(cls, columns: dict[str, np.ndarray]) -> "Baselines":
        once = columns["has_prior"] == 0  # exactly one such row per observation
        labels = columns["label"][once]
        if len(labels) == 0:
            return cls()
        by_train_station = defaultdict(list)
        by_hour_weekday = defaultdict(list)
        keys = zip(
            columns["train_id"][once],
            columns["station_code"][once],
            columns["hour"][once],
            columns["weekday"][once],
            columns["direction"][once],
            labels,
            strict=True,
        )
        for train_id, station, hour, weekday, direction, label in keys:
            by_train_station[(train_id, station)].append(label)
            by_hour_weekday[(int(hour), int(weekday), direction)].append(label)
        return cls(
            overall=float(np.median(labels)),
            by_train_station={k: float(median(v)) for k, v in by_train_station.items()},
            by_hour_weekday={k: float(median(v)) for k, v in by_hour_weekday.items()},
        )

    def predict(self, name: str, columns: dict[str, np.ndarray]) -> np.ndarray:
        n = len(columns["train_id"])
        if name == "zero":
            return np.zeros(n)
        hourly = np.array(
            [
                self.by_hour_weekday.get((int(hour), int(weekday), direction), self.overall)
                for hour, weekday, direction in zip(
                    columns["hour"], columns["weekday"], columns["direction"], strict=True
                )
            ]
        )
        if name == "hour_weekday":
            return hourly
        if name == "train_station":
            return np.array(
                [
                    self.by_train_station.get((train_id, station), fallback)
                    for train_id, station, fallback in zip(
                        columns["train_id"], columns["station_code"], hourly, strict=True
                    )
                ]
            )
        raise ValueError(f"unknown baseline {name!r}")
