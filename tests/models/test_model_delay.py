"""Training, evaluation, saving and prediction, on a small SYNTHETIC database."""

import json
from datetime import date, datetime

import duckdb
import numpy as np
import pytest
from conftest import SAMPLE_CSV

from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.models import features
from sitt.models.__main__ import main
from sitt.models.baselines import Baselines
from sitt.models.delay import (
    DelayModel,
    ModelError,
    TrainConfig,
    load_if_present,
    metrics,
    predict_targets,
    train,
)
from sitt.models.features import FEATURES, Target
from sitt.models.report import SYNTHETIC_HEADING, render_report
from sitt.synth import build_database

START = date(2026, 6, 1)
QUICK = TrainConfig(test_weeks=1, valid_weeks=1, rounds=40)


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory):
    """Six invented weeks for the sample timetable's 13 trains."""
    folder = tmp_path_factory.mktemp("synthetic")
    timetable = folder / "timetable.duckdb"
    with init_db(timetable) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    db = folder / "synthetic.duckdb"
    build_database(timetable, db, folder / "out", START, weeks=6, seed=5)
    return db


@pytest.fixture(scope="module")
def model(synthetic_db):
    with duckdb.connect(str(synthetic_db), read_only=True) as con:
        return train(con, QUICK, source_label="test")


def test_split_is_by_time_and_recorded(model):
    periods = model.metadata["periods"]
    assert periods["train"] == ["2026-06-01", "2026-06-28"]
    assert periods["validation"] == ["2026-06-29", "2026-07-05"]
    assert periods["test"] == ["2026-07-06", "2026-07-12"]
    rows = model.metadata["rows"]
    assert rows["train"] > rows["test"] > 0
    assert model.metadata["features"] == list(FEATURES)


def test_metadata_says_the_data_was_synthetic(model):
    assert model.synthetic is True
    assert model.metadata["sources"] == ["synthetic"]
    assert model.metadata["trained_on"] == "test"


def test_metrics_compare_the_model_with_every_baseline(model):
    results = model.metadata["metrics"]
    assert set(results) == {"all", "no_live_reading", "live_reading"}
    for segment in results.values():
        assert set(segment) == {"model", "zero", "train_station", "hour_weekday"}
        for name, values in segment.items():
            assert values["rows"] > 0 and values["mae"] >= 0
            assert 0 <= values["within_2"] <= values["within_5"] <= 1
            assert ("range_coverage" in values) == (name == "model")
    everything = results["all"]["model"]
    assert results["all"]["zero"]["rows"] == everything["rows"]
    assert (
        results["no_live_reading"]["model"]["rows"] + results["live_reading"]["model"]["rows"]
        == everything["rows"]
    )
    total = everything["range_coverage"] + everything["below_range"] + everything["above_range"]
    assert total == pytest.approx(1)
    # Even this tiny model should do better than assuming every train is on time.
    assert everything["mae"] < results["all"]["zero"]["mae"]


def test_too_little_data_is_refused(synthetic_db):
    with (
        duckdb.connect(str(synthetic_db), read_only=True) as con,
        pytest.raises(ModelError, match="need more than 42"),
    ):
        train(con, TrainConfig(test_weeks=4, valid_weeks=2))


def test_save_load_and_predict(model, synthetic_db, tmp_path):
    model.save(tmp_path / "delay")
    assert {p.name for p in (tmp_path / "delay").iterdir()} == {
        "point.txt",
        "q10.txt",
        "q90.txt",
        "metadata.json",
    }
    loaded = DelayModel.load(tmp_path / "delay")
    assert loaded.synthetic and loaded.metadata["periods"] == model.metadata["periods"]

    monday = date(2026, 7, 13)  # the day after the data ends
    targets = [
        Target("central-90104", monday, "CSMT", datetime(2026, 7, 13, 6, 0)),
        Target("central-90106", monday, "TNA", datetime(2026, 7, 13, 6, 0)),
        Target("central-90113", monday, "KYN", datetime(2026, 7, 13, 6, 0)),  # doesn't go there
        Target("central-unknown", monday, "KYN", datetime(2026, 7, 13, 6, 0)),
    ]
    with duckdb.connect(str(synthetic_db), read_only=True) as con:
        features.prepare(con)
        predictions = predict_targets(con, loaded, targets)
        assert predict_targets(con, loaded, []) == []
        again = predict_targets(con, model, targets)
    assert predictions[2] is None and predictions[3] is None
    for prediction in predictions[:2]:
        assert prediction.low <= prediction.minutes <= prediction.high
        assert -5 < prediction.minutes < 30
    assert predictions[0] == again[0]  # saving and loading changes nothing


def test_load_if_present(model, tmp_path):
    assert load_if_present(tmp_path / "nothing") is None
    model.save(tmp_path / "delay")
    assert load_if_present(tmp_path / "delay") is not None
    # A model saved with a different feature list is not used.
    path = tmp_path / "delay" / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["features"] = ["something_else"]
    path.write_text(json.dumps(metadata), encoding="utf-8")
    assert load_if_present(tmp_path / "delay") is None
    with pytest.raises(ModelError, match="different features"):
        DelayModel.load(tmp_path / "delay")


def test_report_is_headed_synthetic(model):
    text = render_report(model.metadata)
    assert text.startswith(f"# Delay model results ({SYNTHETIC_HEADING})")
    assert "not** an estimate of how well any of this predicts real trains" in text
    assert "| Test | 2026-07-06 to 2026-07-12 |" in text
    assert "Baseline: median by train and station" in text
    assert "Calibration of the 10th–90th percentile range" in text

    real = render_report(model.metadata | {"synthetic": False, "sources": ["ntes"]})
    assert "SYNTHETIC" not in real and real.startswith("# Delay model results\n")


def test_cli_train_and_report(synthetic_db, tmp_path, capsys):
    out = tmp_path / "models" / "delay"
    args = ["train", "--db", str(synthetic_db), "--out", str(out)]
    assert main([*args, "--test-weeks", "1", "--valid-weeks", "1", "--rounds", "20"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("SYNTHETIC RESULTS: THESE SAY NOTHING ABOUT REAL TRAINS")
    assert "Baseline: always on time" in printed and (out / "metadata.json").exists()

    page = tmp_path / "results.md"
    assert main(["report", "--model", str(out), "--write", str(page)]) == 0
    assert "SYNTHETIC RESULTS" in page.read_text(encoding="utf-8")

    assert main(["train", "--db", str(tmp_path / "missing.duckdb"), "--out", str(out)]) == 1
    assert main(["report", "--model", str(tmp_path / "missing")]) == 1
    assert "no saved model" in capsys.readouterr().err


def test_metrics():
    actual = np.array([0.0, 2.0, 5.0, 10.0])
    predicted = np.array([0.4, 4.4, 5.0, 3.0])
    low, high = predicted - 3, predicted + 3
    result = metrics(actual, predicted, low, high)
    assert result["rows"] == 4
    assert result["mae"] == pytest.approx((0.4 + 2.4 + 0 + 7) / 4)
    # Predictions are rounded to whole minutes first: 4.4 -> 4, which is within 2 of 2.
    assert (result["within_2"], result["within_5"]) == (0.75, 0.75)
    assert (result["range_coverage"], result["below_range"], result["above_range"]) == (
        0.75,
        0.0,
        0.25,
    )
    assert result["range_width"] == 6
    assert metrics(np.array([]), np.array([])) == {"rows": 0}
    assert "range_coverage" not in metrics(actual, predicted)


def test_baselines_fall_back_to_coarser_history():
    def columns(rows):
        names = ("train_id", "station_code", "hour", "weekday", "direction", "label", "has_prior")
        return {
            name: np.array([row[i] for row in rows], dtype=object if i in (0, 1, 4) else float)
            for i, name in enumerate(names)
        }

    history = columns(
        [
            ("t1", "KYN", 8, 0, "up", 4.0, 0),
            ("t1", "KYN", 8, 0, "up", 6.0, 0),
            ("t1", "KYN", 8, 0, "up", 50.0, 1),  # a second row for one observation: ignored
            ("t2", "TNA", 8, 0, "up", 1.0, 0),
            ("t3", "DR", 20, 5, "down", 9.0, 0),
        ]
    )
    baselines = Baselines.fit(history)
    assert baselines.overall == 5.0
    ask = columns(
        [
            ("t1", "KYN", 8, 0, "up", 0, 0),  # known train and station
            ("t9", "KYN", 8, 0, "up", 0, 0),  # new train: falls back to the hour
            ("t9", "KYN", 3, 2, "down", 0, 0),  # nothing known: overall median
        ]
    )
    assert list(baselines.predict("zero", ask)) == [0, 0, 0]
    assert list(baselines.predict("train_station", ask)) == [5.0, 4.0, 5.0]
    assert list(baselines.predict("hour_weekday", ask)) == [4.0, 4.0, 5.0]
    with pytest.raises(ValueError):
        baselines.predict("nope", ask)
    assert Baselines.fit(columns([])).overall == 0.0
