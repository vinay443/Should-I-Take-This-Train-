"""Simple delay predictors that any model has to beat.

    zero                 every train is on time
    train_station        the median delay seen before for this train at this station
    hour_weekday         the median delay seen before at this hour, weekday and direction

Each falls back to the next coarser one when it has no history for a key, and finally
to the overall median. They are fitted on one row per observation, so an observation
isn't counted once per earlier reading of its trip.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

import numpy as np

BASELINES = ("zero", "train_station", "hour_weekday")


def _one_row_per_observation(columns: dict[str, np.ndarray]) -> np.ndarray:
    """A mask keeping the first feature row of each observation.

    An observation is a (train, station, time it was read). The feature builder makes
    several rows from each, so counting rows would weight an observation by how many
    earlier readings its trip had.

    This used to pick the rows with no earlier reading (`has_prior == 0`), which is one
    per observation only when a train is never read before it starts. NTES lists a train
    up to two hours ahead, so with its data almost no row qualified and every baseline
    quietly fell back to "always on time".
    """
    if "label_time" not in columns:  # hand-built columns: fall back to the old rule
        return columns["has_prior"] == 0
    seen: set[tuple] = set()
    mask = np.zeros(len(columns["label"]), dtype=bool)
    keys = zip(columns["train_id"], columns["station_code"], columns["label_time"], strict=True)
    for index, key in enumerate(keys):
        if key not in seen:
            seen.add(key)
            mask[index] = True
    return mask


@dataclass
class Baselines:
    overall: float = 0.0
    by_train_station: dict[tuple[str, str], float] = field(default_factory=dict)
    by_hour_weekday: dict[tuple[int, int, str], float] = field(default_factory=dict)

    @classmethod
    def fit(cls, columns: dict[str, np.ndarray]) -> "Baselines":
        once = _one_row_per_observation(columns)
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
