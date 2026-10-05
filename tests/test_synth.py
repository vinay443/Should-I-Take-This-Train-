"""The synthetic generator: reproducible, collector-shaped, and never mistaken for real data."""

from dataclasses import replace
from datetime import UTC, date, datetime

import duckdb
import pytest
from conftest import SAMPLE_CSV

from sitt.db import init_db
from sitt.ingest.live.common import Observation
from sitt.ingest.live.storage import COLUMNS, write_parquet
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.routes import load_routes
from sitt.synth import SOURCE, SynthConfig, SynthError, build_database, simulate, write_batches
from sitt.synth.__main__ import main
from sitt.synth.generate import MARKER_FILE, BlockTemplate

MONDAY = date(2026, 6, 1)


@pytest.fixture
def timetable_db(tmp_path):
    path = tmp_path / "timetable.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    return path


@pytest.fixture
def routes(loaded):
    seqs = dict(loaded.execute("SELECT code, seq FROM stations").fetchall())
    return load_routes(loaded), seqs


def _rows(data):
    return [(batch, o) for batch, observations in data.batches.items() for o in observations]


def test_same_seed_same_data(routes):
    first = simulate(*routes, MONDAY, 3, seed=7)
    again = simulate(*routes, MONDAY, 3, seed=7)
    other = simulate(*routes, MONDAY, 3, seed=8)
    assert first.batches == again.batches
    assert first.batches != other.batches
    assert first.observations > 100


def test_every_reading_is_labelled_synthetic_and_looks_like_a_collector_reading(routes):
    data = simulate(*routes, MONDAY, 7, seed=1)
    stations = set(routes[1])
    events = set()
    for batch_id, observation in _rows(data):
        assert batch_id.endswith("-synth0")
        assert observation.source == SOURCE == "synthetic"
        assert observation.raw_status is None
        assert observation.observed_at.utcoffset() is not None
        events.add(observation.event)
        if observation.cancelled:
            assert (observation.event, observation.station_code) == ("cancelled", "")
            assert observation.delay_minutes is None
        else:
            assert observation.station_code in stations
            assert observation.delay_minutes == round(observation.delay_minutes)
            assert observation.delay_minutes >= -4  # earliest, plus reading noise
        assert (observation.time_kind == "actual") == (observation.event == "at")
    assert events >= {"at", "arriving", "crossed", "between"}
    # Polls fall on the collector's 15-minute grid.
    assert {o.observed_at.minute % 15 for _, o in _rows(data)} == {0}


def test_parquet_files_match_the_collectors(routes, tmp_path):
    data = simulate(*routes, MONDAY, 1, seed=1)
    write_batches(data.batches, tmp_path / "synthetic" / "observations")
    reading = Observation(datetime(2026, 6, 1, 3, 0, tzinfo=UTC), "90101", "KYN", "at", "mobond")
    real = write_parquet([reading], tmp_path / "real", "20260601T030000Z-abc123")

    files = sorted((tmp_path / "synthetic" / "observations").rglob("*.parquet"))
    assert len(files) == len(data.batches)
    first_batch = next(iter(data.batches))
    assert files[0].parent.name == "2026-05-31"  # folders are UTC dates, as the collector's are
    assert files[0].stem == first_batch
    assert (
        (tmp_path / "synthetic" / MARKER_FILE)
        .read_text(encoding="utf-8")
        .startswith("SYNTHETIC DATA")
    )

    with duckdb.connect() as con:
        describe = "DESCRIBE SELECT * FROM read_parquet(?)"
        ours = con.execute(describe, [str(files[0])]).fetchall()
        theirs = con.execute(describe, [str(real)]).fetchall()
        assert ours == theirs
        assert [row[0] for row in ours] == list(COLUMNS)
        (count,) = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [[f.as_posix() for f in files]]
        ).fetchone()
    assert count == data.observations


def test_megablock_sunday_is_recorded_and_slows_trains(routes):
    sunday = date(2026, 6, 7)
    # The sample timetable's trains run early in the morning, so block the whole line all day.
    all_day = (BlockTemplate("CSMT", "KYN", "slow", "00:00", "23:59"),)
    calm = replace(SynthConfig(), megablock_chance=0.0)
    blocked = replace(
        SynthConfig(),
        megablock_chance=1.0,
        megablocks=all_day,
        megablock_step_delay=(3.0, 3.0),
        cancel_chance_in_block=0.0,
    )

    without = simulate(*routes, sunday, 1, calm, seed=3)
    with_block = simulate(*routes, sunday, 1, blocked, seed=3)

    assert without.blocks == []
    [block] = with_block.blocks
    assert (block["block_date"], block["source"], block["line"]) == (sunday, SOURCE, "central")
    assert (block["from_station"], block["to_station"], block["tracks"]) == ("CSMT", "KYN", "slow")
    assert "SYNTHETIC" in block["summary"]

    slow = {route.number for route in routes[0] if route.train_type == "slow"}

    def worst(data, numbers):
        return max(o.delay_minutes for _, o in _rows(data) if o.train_number in numbers)

    assert worst(with_block, slow) > 30  # 3 minutes at each of 25 stations
    assert worst(without, slow) < 20
    fast = {route.number for route in routes[0]} - slow
    assert worst(with_block, fast) < 20  # the block is on the slow lines only
    # No megablocks on other days of the week.
    assert simulate(*routes, MONDAY, 6, blocked, seed=3).blocks == []


def test_cancelled_trains_report_only_cancellations(routes):
    everything_cancelled = replace(SynthConfig(), cancel_chance=1.0, megablock_chance=0.0)
    data = simulate(*routes, MONDAY, 1, everything_cancelled, seed=1)
    assert data.cancelled_runs == data.runs > 0
    assert {o.event for _, o in _rows(data)} == {"cancelled"}


def test_build_database(timetable_db, tmp_path):
    db = tmp_path / "synthetic.duckdb"
    result = build_database(timetable_db, db, tmp_path / "synthetic", MONDAY, weeks=1, seed=2)

    assert result.observations > 300 and result.days == 7
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM trains").fetchone() == (13,)
        sources = con.execute("SELECT DISTINCT source FROM observations").fetchall()
        assert sources == [(SOURCE,)]
        (rows, matched) = con.execute(
            "SELECT count(*), count(*) FILTER (train_id LIKE 'central-%') FROM observations"
        ).fetchone()
        assert rows == matched == result.observations  # the loader matched every train
        assert con.execute("SELECT count(*) FROM blocks").fetchone() == (result.blocks,)

    # Running it again replaces the database rather than adding to it.
    again = build_database(timetable_db, db, tmp_path / "synthetic", MONDAY, weeks=1, seed=2)
    assert again.observations == result.observations
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM observations").fetchone() == (rows,)


def test_never_writes_into_a_real_database(timetable_db, tmp_path):
    with pytest.raises(SynthError, match="that is the real database"):
        build_database(timetable_db, timetable_db, tmp_path / "out", MONDAY, weeks=1)

    # A database holding anything real is not overwritten either.
    other = tmp_path / "other.duckdb"
    with init_db(other) as con:
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, source) "
            "VALUES (now(), '95001', 'KYN', 'ntes')"
        )
    with pytest.raises(SynthError, match="1 non-synthetic observations"):
        build_database(timetable_db, other, tmp_path / "out", MONDAY, weeks=1)

    # Nor is a folder of Parquet files that the generator didn't write.
    (tmp_path / "collected" / "observations").mkdir(parents=True)
    with pytest.raises(SynthError, match="refusing to clear"):
        build_database(timetable_db, tmp_path / "s.duckdb", tmp_path / "collected", MONDAY, 1)


def test_cli(timetable_db, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SITT_DB_PATH", str(timetable_db))
    db = tmp_path / "synthetic.duckdb"
    code = main(["--weeks", "1", "--db", str(db), "--out", str(tmp_path / "synthetic")])
    assert code == 0
    printed = capsys.readouterr().out
    assert printed.startswith("SYNTHETIC data written to")
    assert "say nothing about real trains" in printed

    assert main(["--weeks", "1", "--db", str(timetable_db), "--out", str(tmp_path / "x")]) == 1
    assert "real database" in capsys.readouterr().err
