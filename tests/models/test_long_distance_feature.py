"""The long-distance congestion feature: computed without leaks, optional in the model.

Readings here are invented (source 'test') for the invented sample timetable. Train
90104 is a slow up train: Kalyan 07:03, Kalwa 07:27, Kurla 07:58. Numbers such as 12127
are not in that timetable, which is what makes a reading "long-distance".
"""

from datetime import date, datetime

import duckdb
import numpy as np
import pytest
from conftest import SAMPLE_CSV

from sitt import config, dq
from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.models import __main__ as cli
from sitt.models import features
from sitt.models.delay import DelayModel, ModelError, TrainConfig, train
from sitt.models.features import FEATURES, LONG_DISTANCE_FEATURES, OPTIONAL_FEATURES, Target
from sitt.synth.generate import SynthConfig, simulate
from sitt.tz import IST

MONDAY = date(2026, 9, 28)


def at(hhmm: str, day: date = MONDAY) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=IST)


def add(con, seen: str, number: str, delay: float | None, station="KYN", event="arrival",
        cancelled=False) -> int:  # fmt: skip
    """One reading in the batch taken at `seen`. A 9xxxx number is a local in the timetable."""
    local = number.startswith("9")
    (row_id,) = con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source, "
        "train_number, event, batch_id, cancelled) VALUES (?, ?, ?, ?, 'test', ?, ?, ?, ?) "
        "RETURNING id",
        [at(seen), f"central-{number}" if local else number, station, delay, number, event,
         f"batch-{seen}", cancelled],
    ).fetchone()  # fmt: skip
    return row_id


def features_at(con, cutoff: str, station="CLA") -> dict:
    features.prepare(con)
    features.set_targets(con, [Target("central-90104", MONDAY, station, at(cutoff))])
    columns = features.build_features(con)
    return {name: columns[name][0] for name in (*LONG_DISTANCE_FEATURES, "line_median_delay")}


@pytest.fixture
def board(loaded):
    """Two polls of a Kalyan board: locals and long-distance trains together."""
    add(loaded, "07:00", "90104", 2)
    add(loaded, "07:00", "12127", 10)  # arrival and departure of one train: counted once,
    add(loaded, "07:00", "12127", 14, event="departure")  # at their average, 12
    add(loaded, "07:00", "11072", 40)
    add(loaded, "07:00", "22105", 6)
    add(loaded, "07:15", "90104", 3, station="DI")
    add(loaded, "07:15", "12127", 60)
    add(loaded, "07:15", "11072", 90)
    return loaded


def test_long_distance_features_are_not_standard_model_inputs():
    assert LONG_DISTANCE_FEATURES == ("ld_median_delay", "ld_count")
    assert set(OPTIONAL_FEATURES).isdisjoint(FEATURES)


def test_the_median_delay_of_long_distance_trains_in_the_last_batch(board):
    seen = features_at(board, "07:05")
    # Three long-distance trains at 07:00: 12 (the average of 10 and 14), 40 and 6.
    assert (seen["ld_median_delay"], seen["ld_count"]) == (12.0, 3.0)
    # The local's own reading is not part of it: the line's median is separate.
    assert seen["line_median_delay"] == 2.0


def test_nothing_after_the_cutoff_is_used(board):
    # At 07:14 the 07:15 batch (60 and 90 minutes late) hasn't happened yet.
    assert features_at(board, "07:14")["ld_median_delay"] == 12.0
    after = features_at(board, "07:16")
    assert (after["ld_median_delay"], after["ld_count"]) == (75.0, 2.0)
    # Changing a later batch can't change an earlier row.
    board.execute("UPDATE observations SET delay_minutes = 500 WHERE batch_id = 'batch-07:15'")
    assert features_at(board, "07:14")["ld_median_delay"] == 12.0
    # Before any batch there is nothing to know.
    early = features_at(board, "06:30")
    assert np.isnan(early["ld_median_delay"]) and early["ld_count"] == 0.0


def test_a_stale_batch_is_not_used(board):
    # The last batch was at 07:15. Twenty minutes is the limit for "the state of the line".
    assert features_at(board, "07:35")["ld_median_delay"] == 75.0
    stale = features_at(board, "07:36")
    assert np.isnan(stale["ld_median_delay"]) and stale["ld_count"] == 0.0


def test_cancelled_missing_and_flagged_readings_are_left_out(board):
    add(board, "07:00", "16345", None)  # no delay given
    add(board, "07:00", "17617", 300, cancelled=True)
    absurd = add(board, "07:00", "20103", 5000)  # more than a day late
    assert features_at(board, "07:05")["ld_count"] == 4.0  # the absurd one still counts...
    dq.refresh_flags(board, now=at("12:00"), include_synthetic=True)
    assert (absurd,) in board.execute("SELECT observation_id FROM dq_flags").fetchall()
    seen = features_at(board, "07:05")
    assert (seen["ld_median_delay"], seen["ld_count"]) == (12.0, 3.0)  # ...until it is flagged


def test_a_database_with_no_long_distance_trains_gives_empty_features(loaded):
    add(loaded, "07:00", "90104", 2)
    seen = features_at(loaded, "07:05")
    assert np.isnan(seen["ld_median_delay"]) and seen["ld_count"] == 0.0


# --- the model treats them as optional ---


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory):
    """SYNTHETIC observations, with invented long-distance trains at Kalyan switched on."""
    path = tmp_path_factory.mktemp("ld") / "synthetic.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        from sitt.ingest.live.storage import to_row
        from sitt.routes import load_routes

        seqs = dict(con.execute("SELECT code, seq FROM stations").fetchall())
        data = simulate(
            load_routes(con), seqs, date(2026, 6, 1), 28, SynthConfig(long_distance_trains=24)
        )
        rows = [
            to_row(observation, batch_id)
            for batch_id, batch in data.batches.items()
            for observation in batch
        ]
        con.executemany(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
            "source, train_number, event, batch_id, cancelled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [r["observed_at"], r["train_id"], r["station_code"], r["delay_minutes"],
                 r["source"], r["train_number"], r["event"], r["batch_id"], r["cancelled"]]
                for r in rows
            ],
        )  # fmt: skip
        con.execute(
            "UPDATE observations SET train_id = 'central-' || train_number "
            "WHERE train_number IN (SELECT number FROM trains)"
        )
    return path


def test_the_synthetic_generator_only_adds_long_distance_trains_when_asked(synthetic_db):
    with duckdb.connect(str(synthetic_db), read_only=True) as con:
        sources, stations, trains = con.execute(
            "SELECT count(DISTINCT source), count(DISTINCT station_code), "
            "count(DISTINCT train_number) FROM observations "
            "WHERE train_id NOT IN (SELECT train_id FROM trains)"
        ).fetchone()
        assert con.execute("SELECT DISTINCT source FROM observations").fetchall() == [
            ("synthetic",)
        ]
        routes_and_seqs = None  # (the default configuration is checked below)
    assert (sources, stations, trains) == (1, 1, 24)
    assert routes_and_seqs is None

    with init_db(":memory:") as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        from sitt.routes import load_routes

        seqs = dict(con.execute("SELECT code, seq FROM stations").fetchall())
        routes = load_routes(con)
        plain = simulate(routes, seqs, date(2026, 6, 1), 3)
        again = simulate(routes, seqs, date(2026, 6, 1), 3, SynthConfig(long_distance_trains=24))
    numbers = {o.train_number for batch in plain.batches.values() for o in batch}
    assert not any(number.startswith("11") for number in numbers)
    # Switching it on adds readings and changes none of the others.
    locals_only = {
        batch_id: [o for o in batch if not o.train_number.startswith("11")]
        for batch_id, batch in again.batches.items()
    }
    assert {k: v for k, v in locals_only.items() if v} == plain.batches


def test_a_model_can_be_trained_with_or_without_the_feature(synthetic_db, tmp_path):
    split = TrainConfig(test_weeks=1, valid_weeks=1, rounds=40)
    with duckdb.connect(str(synthetic_db), read_only=True) as con:
        plain = train(con, split, "test")
        extended = train(
            con, TrainConfig(**{**vars(split), "extra_features": LONG_DISTANCE_FEATURES}), "test"
        )
        with pytest.raises(ModelError, match="unknown extra feature"):
            train(con, TrainConfig(**{**vars(split), "extra_features": ("made_up",)}), "test")
        has_value = con.execute("SELECT count(*) FROM feature_rows WHERE ld_count > 0").fetchone()
    assert plain.metadata["features"] == list(FEATURES)
    assert extended.metadata["features"] == [*FEATURES, *LONG_DISTANCE_FEATURES]
    assert plain.synthetic and extended.synthetic
    assert has_value[0] > 0  # the feature really reaches the rows
    assert {name for name, _ in extended.metadata["importance"]} >= set(LONG_DISTANCE_FEATURES)
    assert extended.metadata["config"]["extra_features"] == list(LONG_DISTANCE_FEATURES)

    # Both kinds save, load and predict; a model with unknown inputs is refused.
    for name, model in (("plain", plain), ("extended", extended)):
        model.save(tmp_path / name)
        loaded = DelayModel.load(tmp_path / name)
        assert loaded.metadata["features"] == model.metadata["features"]
    metadata_path = tmp_path / "extended" / "metadata.json"
    metadata_path.write_text(
        metadata_path.read_text("utf-8").replace('"ld_count"', '"something_else"'), "utf-8"
    )
    with pytest.raises(ModelError, match="different features"):
        DelayModel.load(tmp_path / "extended")


def test_ablate_prints_a_synthetic_labelled_comparison(synthetic_db, capsys):
    code = cli.main(
        ["ablate", "--db", str(synthetic_db), "--test-weeks", "1", "--valid-weeks", "1"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("SYNTHETIC RESULTS: THESE SAY NOTHING ABOUT REAL TRAINS\n")
    assert "Feature group: long_distance (ld_median_delay, ld_count)" in out
    assert "without the feature" in out and "with the feature" in out
    assert "MAE change with the feature:" in out
    assert "it is not a finding" in out and "says nothing about real trains" in out


def test_the_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SITT_FEATURE_LONG_DISTANCE", raising=False)
    assert config.long_distance_feature_enabled() is False
    monkeypatch.setenv("SITT_FEATURE_LONG_DISTANCE", "true")
    assert config.long_distance_feature_enabled() is True
