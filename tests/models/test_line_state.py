"""Backtest of the line-state nowcast (sitt.models.line_state)."""

from datetime import datetime, timedelta

from conftest import SAMPLE_CSV

from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.models import line_state
from sitt.tz import IST

DAY = datetime(2026, 10, 6, tzinfo=IST)


def at(hhmm: str) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return DAY + timedelta(hours=hours, minutes=minutes)


def reading(con, train, observed, expected, delay):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, actual_or_expected_time, "
        "time_kind, delay_minutes, source, event) VALUES (?, ?, 'KYN', ?, 'expected', ?, 'ntes', "
        "'departure')",
        [at(observed), train, at(expected), delay],
    )


def test_a_late_line_predicts_a_train_that_still_reads_on_time(loaded):
    # 90104 (07:03 up) arrives 10 late: read close to its time, so it sets the line state.
    reading(loaded, "central-90104", "07:05", "07:13", 10)
    # 90106 (07:12 up) still reads on time at 07:05, then turns out 10 late.
    reading(loaded, "central-90106", "07:05", "07:12", 0)
    reading(loaded, "central-90106", "07:20", "07:22", 10)

    rows = {row.lead: row for row in line_state.backtest(loaded, leads=(15,))}
    row = rows[15]
    # Two runs have a final reading: 90104 (no other train known before it) and 90106.
    assert row.runs == 2
    # For 90106, asked at 07:07: own reading 0 (error 10), line state 10 (error 0).
    # For 90104, asked at 06:58: nothing known yet; final 10, so every predictor is 10 off.
    assert row.mae == {"zero": 10.0, "own": 10.0, "line": 5.0, "max": 5.0}
    assert row.with_line == 0.5
    table = line_state.format_table([row])
    assert "| 15 min | 2 | 50% | 10.0 | 10.0 | **5.0** | **5.0** |" in table


def test_by_direction_and_cli(tmp_path, capsys):
    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    assert line_state.main(["--db", str(db)]) == 1
    with init_db(db) as con:
        reading(con, "central-90106", "07:05", "07:12", 0)
        reading(con, "central-90106", "07:10", "07:14", 2)
    assert line_state.main(["--db", str(db), "--by-direction"]) == 0
    out = capsys.readouterr().out
    assert "| Asked before due | Direction |" in out and "| 15 min | up | 1 |" in out
    assert line_state.main(["--db", str(tmp_path / "missing.duckdb")]) == 2
