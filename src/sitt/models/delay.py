"""Train, evaluate, save and use the LightGBM delay model.

Three boosters are trained on the rows from `sitt.models.features`: one for the likely
delay (median) and two quantile models for a rough 10th-90th percentile range.

Evaluation is by time only: the last `test_weeks` of data are held out, the
`valid_weeks` before them stop training early, and everything earlier is trained on.
Rows are never shuffled between those periods, because a random split would let the
model see the same day's disruption in both training and test.

A saved model is a folder:

    point.txt, q10.txt, q90.txt    the boosters
    metadata.json                  what it was trained on, its metrics, and whether the
                                   data was synthetic
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np

from sitt.models import features
from sitt.models.baselines import BASELINES, Baselines
from sitt.models.features import CATEGORICAL, FEATURES, OPTIONAL_FEATURES, Target

DEFAULT_MODEL_DIR = Path("models/delay")
QUANTILES = {"q10": 0.1, "q90": 0.9}
METADATA_FILE = "metadata.json"
SYNTHETIC_SOURCE = "synthetic"

PARAMS = {
    "learning_rate": 0.1,
    "num_leaves": 63,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.8,
    "lambda_l2": 1.0,
    "max_cat_to_onehot": 4,
    "cat_smooth": 20,
    "verbose": -1,
    "deterministic": True,
    "force_col_wise": True,
}


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrainConfig:
    test_weeks: int = 4
    valid_weeks: int = 2
    rounds: int = 800
    early_stopping: int = 30
    seed: int = 1
    max_train_rows: int | None = None  # sample the training rows down to this many
    # Experimental inputs to add to the standard ones (from features.OPTIONAL_FEATURES).
    extra_features: tuple[str, ...] = ()


def encode(
    columns: dict[str, np.ndarray],
    categories: dict[str, list[str]],
    feature_names: tuple[str, ...] | list[str] = FEATURES,
) -> np.ndarray:
    """Feature matrix in `feature_names` order. Unknown categories become -1 (missing)."""
    n = len(columns[feature_names[0]])
    matrix = np.empty((n, len(feature_names)))
    for j, name in enumerate(feature_names):
        if name in CATEGORICAL:
            codes = {value: i for i, value in enumerate(categories[name])}
            matrix[:, j] = [codes.get(value, -1) for value in columns[name]]
        else:
            matrix[:, j] = columns[name]
    return matrix


def metrics(
    actual: np.ndarray,
    predicted: np.ndarray,
    low: np.ndarray | None = None,
    high: np.ndarray | None = None,
) -> dict[str, float]:
    """MAE, share within 2 and 5 minutes, and how well the range holds the truth.

    Sources report delays in whole minutes, so "within N minutes" compares the
    prediction rounded to a whole minute. Otherwise a prediction of 4.3 for a delay of
    2 would count as a miss while a prediction of 4 counts as a hit.
    """
    if len(actual) == 0:
        return {"rows": 0}
    error = np.abs(actual - predicted)
    rounded_error = np.abs(actual - np.round(predicted))
    out = {
        "rows": int(len(actual)),
        "mae": float(error.mean()),
        "within_2": float((rounded_error <= 2).mean()),
        "within_5": float((rounded_error <= 5).mean()),
    }
    if low is not None and high is not None:
        # Pinball loss of the two ends of the range: lower is better, and unlike coverage
        # it also rewards a range that is no wider than it needs to be.
        out["pinball_q10"] = pinball_loss(actual, low, QUANTILES["q10"])
        out["pinball_q90"] = pinball_loss(actual, high, QUANTILES["q90"])
        out["range_coverage"] = float(((actual >= low) & (actual <= high)).mean())
        out["below_range"] = float((actual < low).mean())
        out["above_range"] = float((actual > high).mean())
        out["range_width"] = float((high - low).mean())
    return out


def pinball_loss(actual: np.ndarray, predicted: np.ndarray, alpha: float) -> float:
    """Mean quantile (pinball) loss of `predicted` as the `alpha` quantile of `actual`."""
    error = actual - predicted
    return float(np.maximum(alpha * error, (alpha - 1) * error).mean())


def _subset(columns: dict[str, np.ndarray], mask: np.ndarray) -> dict[str, np.ndarray]:
    return {name: values[mask] for name, values in columns.items()}


def _day(value: np.datetime64) -> date:
    return value.astype("datetime64[D]").astype(date)


@dataclass
class DelayModel:
    boosters: dict[str, lgb.Booster]
    metadata: dict

    @property
    def synthetic(self) -> bool:
        return bool(self.metadata.get("synthetic"))

    def predict(self, columns: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """`point`, `low` and `high` delay in minutes for each row."""
        matrix = encode(
            columns, self.metadata["categories"], self.metadata.get("features") or FEATURES
        )
        point = self.boosters["point"].predict(matrix)
        q10 = self.boosters["q10"].predict(matrix)
        q90 = self.boosters["q90"].predict(matrix)
        # Separate models can cross; keep low <= point <= high.
        return {
            "point": point,
            "low": np.minimum(np.minimum(q10, q90), point),
            "high": np.maximum(np.maximum(q10, q90), point),
        }

    def save(self, model_dir: Path) -> None:
        model_dir.mkdir(parents=True, exist_ok=True)
        for name, booster in self.boosters.items():
            booster.save_model(str(model_dir / f"{name}.txt"))
        (model_dir / METADATA_FILE).write_text(
            json.dumps(self.metadata, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, model_dir: Path = DEFAULT_MODEL_DIR) -> "DelayModel":
        metadata_path = Path(model_dir) / METADATA_FILE
        if not metadata_path.exists():
            raise ModelError(f"no saved model in {model_dir}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        saved = metadata.get("features") or []
        standard, extra = saved[: len(FEATURES)], saved[len(FEATURES) :]
        if standard != list(FEATURES) or not set(extra) <= set(OPTIONAL_FEATURES):
            raise ModelError(
                f"the model in {model_dir} was trained on different features; train it again"
            )
        boosters = {
            name: lgb.Booster(model_file=str(Path(model_dir) / f"{name}.txt"))
            for name in ("point", *QUANTILES)
        }
        return cls(boosters, metadata)


def load_if_present(model_dir: Path = DEFAULT_MODEL_DIR) -> DelayModel | None:
    """The saved model, or None if there isn't a usable one."""
    try:
        return DelayModel.load(model_dir)
    except (ModelError, OSError, ValueError, lgb.basic.LightGBMError):
        return None


def train(
    con: duckdb.DuckDBPyConnection, config: TrainConfig | None = None, source_label: str = ""
) -> DelayModel:
    """Build features from `con`, train on the earlier weeks and evaluate on the last ones."""
    config = config or TrainConfig()
    features.prepare(con)
    sources = [row[0] for row in con.execute("SELECT DISTINCT source FROM obs").fetchall()]
    if not sources:
        raise ModelError("no usable observations to train on")
    features.training_targets(con)
    columns = features.build_features(con)

    days = columns["service_day"].astype("datetime64[D]")
    first, last = days.min(), days.max()
    test_start = last - np.timedelta64(config.test_weeks * 7 - 1, "D")
    valid_start = test_start - np.timedelta64(config.valid_weeks * 7, "D")
    if valid_start <= first:
        raise ModelError(
            f"only {(last - first).astype(int) + 1} days of data: need more than "
            f"{(config.test_weeks + config.valid_weeks) * 7} to hold out validation and test weeks"
        )
    train_mask = days < valid_start
    valid_mask = (days >= valid_start) & (days < test_start)
    test_mask = days >= test_start

    rng = np.random.default_rng(config.seed)
    if config.max_train_rows and train_mask.sum() > config.max_train_rows:
        keep = rng.choice(np.flatnonzero(train_mask), config.max_train_rows, replace=False)
        train_mask = np.zeros_like(train_mask)
        train_mask[keep] = True

    train_columns = _subset(columns, train_mask)
    valid_columns = _subset(columns, valid_mask)
    test_columns = _subset(columns, test_mask)
    categories = {name: sorted(set(train_columns[name])) for name in CATEGORICAL}
    unknown = set(config.extra_features) - set(OPTIONAL_FEATURES)
    if unknown:
        raise ModelError(f"unknown extra feature(s): {', '.join(sorted(unknown))}")
    feature_names = [*FEATURES, *config.extra_features]
    categorical = [feature_names.index(name) for name in CATEGORICAL]

    def dataset(part: dict[str, np.ndarray], reference: lgb.Dataset | None = None) -> lgb.Dataset:
        return lgb.Dataset(
            encode(part, categories, feature_names),
            label=part["label"],
            feature_name=feature_names,
            categorical_feature=categorical,
            reference=reference,
            free_raw_data=False,
        )

    train_set = dataset(train_columns)
    valid_set = dataset(valid_columns, train_set)
    objectives = {"point": {"objective": "regression_l1"}} | {
        name: {"objective": "quantile", "alpha": alpha} for name, alpha in QUANTILES.items()
    }
    boosters = {}
    for name, objective in objectives.items():
        boosters[name] = lgb.train(
            PARAMS | objective | {"seed": config.seed},
            train_set,
            num_boost_round=config.rounds,
            valid_sets=[valid_set],
            callbacks=[lgb.early_stopping(config.early_stopping, verbose=False)],
        )

    baselines = Baselines.fit(train_columns)
    model = DelayModel(boosters, {"categories": categories, "features": feature_names})
    predicted = model.predict(test_columns)
    actual = test_columns["label"]
    cold = test_columns["has_prior"] == 0
    segments = {
        "all": np.ones(len(actual), dtype=bool),
        "no_live_reading": cold,
        "live_reading": ~cold,
    }
    results: dict[str, dict] = {}
    for segment, mask in segments.items():
        results[segment] = {
            "model": metrics(
                actual[mask],
                predicted["point"][mask],
                predicted["low"][mask],
                predicted["high"][mask],
            )
        }
        for baseline in BASELINES:
            results[segment][baseline] = metrics(
                actual[mask], baselines.predict(baseline, test_columns)[mask]
            )

    # Per station: how much there was to learn from and to test on, and how the model did
    # there against the best baseline. Small samples are the reader's to judge.
    by_station: dict[str, dict] = {}
    for station in sorted(set(columns["station_code"])):
        at_station = test_columns["station_code"] == station
        entry: dict = {
            "train_rows": int((train_columns["station_code"] == station).sum()),
            "test_rows": int(at_station.sum()),
        }
        if at_station.any():
            entry["model_mae"] = float(
                np.abs(actual[at_station] - predicted["point"][at_station]).mean()
            )
            entry["baseline_mae"] = {
                baseline: float(
                    np.abs(
                        actual[at_station] - baselines.predict(baseline, test_columns)[at_station]
                    ).mean()
                )
                for baseline in BASELINES
            }
        by_station[station] = entry

    gain = boosters["point"].feature_importance(importance_type="gain")
    importance = sorted(
        zip(feature_names, gain / max(gain.sum(), 1e-9), strict=True), key=lambda x: -x[1]
    )
    model.metadata = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "synthetic": SYNTHETIC_SOURCE in sources,
        "sources": sorted(sources),
        "trained_on": source_label,
        "features": feature_names,
        "categories": categories,
        "periods": {
            "train": [str(_day(first)), str(_day(valid_start) - timedelta(days=1))],
            "validation": [str(_day(valid_start)), str(_day(test_start) - timedelta(days=1))],
            "test": [str(_day(test_start)), str(_day(last))],
        },
        "rows": {
            "train": int(train_mask.sum()),
            "validation": int(valid_mask.sum()),
            "test": int(test_mask.sum()),
        },
        "trees": {name: booster.best_iteration for name, booster in boosters.items()},
        "config": {
            "test_weeks": config.test_weeks,
            "valid_weeks": config.valid_weeks,
            "rounds": config.rounds,
            "early_stopping": config.early_stopping,
            "seed": config.seed,
            "max_train_rows": config.max_train_rows,
            "extra_features": list(config.extra_features),
            "lightgbm": PARAMS,
        },
        "metrics": results,
        "by_station": by_station,
        "importance": [[name, round(float(share), 4)] for name, share in importance],
        "baseline_overall_median": baselines.overall,
    }
    return model


@dataclass(frozen=True)
class DelayPrediction:
    """A predicted delay in minutes, with a rough 10th-90th percentile range."""

    minutes: float
    low: float
    high: float


def predict_targets(
    con: duckdb.DuckDBPyConnection, model: DelayModel, targets: list[Target]
) -> list[DelayPrediction | None]:
    """Predict each target. None where the train doesn't serve the station.

    `features.prepare(con)` must have been called on this connection.
    """
    if not targets:
        return []
    features.set_targets(con, targets)
    columns = features.build_features(con)
    predicted = model.predict(columns)
    out: list[DelayPrediction | None] = [None] * len(targets)
    for i, target_id in enumerate(columns["target_id"]):
        out[int(target_id)] = DelayPrediction(
            float(predicted["point"][i]), float(predicted["low"][i]), float(predicted["high"][i])
        )
    return out
