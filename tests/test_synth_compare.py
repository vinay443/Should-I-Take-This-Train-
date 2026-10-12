"""Comparing synthetic delays with real readings (sitt.synth.compare)."""

from datetime import datetime, timedelta

import pytest
from conftest import SAMPLE_CSV

from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.synth import compare
from sitt.tz import IST

TUESDAY = datetime(2026, 10, 6, tzinfo=IST)


def at(hhmm: str, day: datetime = TUESDAY) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return day + timedelta(hours=hours, minutes=minutes)


@pytest.fixture
def pair():
    with init_db(":memory:") as real, init_db(":memory:") as synthetic:
        for con in (real, synthetic):
            load_timetable(con, read_timetable(SAMPLE_CSV))
        yield real, synthetic


def ntes(con, train, observed, expected, delay):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, actual_or_expected_time, "
        "time_kind, delay_minutes, source, event) VALUES (?, ?, 'KYN', ?, 'expected', ?, "
        "'ntes', 'departure')",
        [at(observed), train, at(expected), delay],
    )


def synth(con, train, observed, station, delay, day=TUESDAY):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source, "
        "event) VALUES (?, ?, ?, ?, 'synthetic', 'at')",
        [at(observed, day), train, station, delay],
    )


def test_compares_the_same_stations_and_hours(pair):
    real, synthetic = pair
    # Real: the 07:12 fast up (90106) read at Kalyan; its last reading near its time counts.
    ntes(real, "central-90106", "06:45", "07:12", 0)
    ntes(real, "central-90106", "07:12", "07:24", 12)
    # Synthetic: the same train on two other Tuesdays, near Kalyan (Thakurli is next to it).
    synth(synthetic, "central-90106", "07:10", "THK", 2, day=TUESDAY - timedelta(days=7))
    synth(synthetic, "central-90106", "07:12", "KYN", 4, day=TUESDAY - timedelta(days=14))
    # ...and at an hour the real data doesn't cover, and far from Kalyan: both left out.
    synth(synthetic, "central-90106", "15:00", "KYN", 30)
    synth(synthetic, "central-90104", "07:50", "DR", 30)

    real_summary, synth_summary, stations, slots = compare.compare(real, synthetic)
    assert stations == {"KYN"}
    assert slots == {(False, 7)}
    assert [(s.direction, s.train_type, s.runs, s.median) for s in real_summary] == [
        ("up", "fast", 1, 12.0)
    ]
    assert [(s.direction, s.train_type, s.runs, s.median) for s in synth_summary] == [
        ("up", "fast", 2, 3.0)
    ]
    table = compare.format_comparison(real_summary, synth_summary)
    assert "| up | fast | real | 1 | 12.0 | 12 |" in table
    assert "| up | fast | synthetic | 2 | 3.0 | 3 |" in table


def test_neighbours_follow_line_order(pair):
    real, _ = pair
    assert compare.neighbours(real, {"KYN"}) >= {"KYN", "THK"}
    assert "DR" not in compare.neighbours(real, {"KYN"})


def test_cli(tmp_path, capsys):
    real_path, synth_path = tmp_path / "real.duckdb", tmp_path / "synth.duckdb"
    for path in (real_path, synth_path):
        with init_db(path) as con:
            load_timetable(con, read_timetable(SAMPLE_CSV))
    args = ["--db", str(real_path), "--synthetic-db", str(synth_path)]
    assert compare.main(args) == 1
    assert "No real runs" in capsys.readouterr().out
    with init_db(real_path) as con:
        ntes(con, "central-90106", "07:12", "07:24", 12)
    assert compare.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("Stations: KYN. Hours: 07 (weekday).")
    assert "| up | fast | synthetic | 0 |" in out
    assert compare.main(["--db", str(tmp_path / "nope.duckdb")]) == 2
