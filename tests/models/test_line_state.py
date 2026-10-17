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


def reading_at(con, train, station, observed, expected, delay, event="departure"):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, actual_or_expected_time, "
        "time_kind, delay_minutes, source, event) VALUES (?, ?, ?, ?, 'expected', ?, 'ntes', ?)",
        [at(observed), train, station, at(expected), delay, event],
    )


def test_a_late_line_predicts_a_train_that_still_reads_on_time(loaded):
    # 90106 (up, due 07:12) is read 10 late at 07:10, close to its time: the line state.
    reading(loaded, "central-90106", "07:10", "07:22", 10)
    # 90108 (up, due 07:31) still reads on time at 07:15, then turns out 10 late.
    reading(loaded, "central-90108", "07:15", "07:31", 0)
    reading(loaded, "central-90108", "07:30", "07:41", 10)

    row = line_state.backtest(loaded, leads=(15,))[0]
    # Asked at 07:16 (15 min before 90108's scheduled 07:31): own 0, line 10, final 10.
    # 90106, asked at 06:57 (before 07:12): nothing known yet, final 10.
    assert row.runs == 2
    assert row.mae == {"zero": 10.0, "own": 10.0, "line": 5.0, "max": 5.0}
    assert row.with_line == 0.5
    table = line_state.format_table([row])
    assert "| 15 min | 2 | 50% | 10.0 | 10.0 | **5.0** | **5.0** |" in table


def test_arrival_and_departure_of_one_run_count_once(loaded):
    reading_at(loaded, "central-90106", "KYN", "07:05", "07:12", 10, event="arrival")
    reading_at(loaded, "central-90106", "KYN", "07:05", "07:13", 10, event="departure")
    assert line_state.backtest(loaded, leads=(15,))[0].runs == 1


def test_each_station_is_its_own_run(loaded):
    reading_at(loaded, "central-90106", "KYN", "07:10", "07:22", 10)
    reading_at(loaded, "central-90106", "TNA", "07:35", "07:46", 12)
    row = line_state.backtest(loaded, leads=(15,))[0]
    assert row.runs == 2  # Kalyan and Thane, never mixed


def test_the_question_is_asked_before_the_scheduled_time_not_the_delayed_one(loaded):
    # 90106 is due at 07:12. Readings at 07:25 and 07:35 show it 25-30 min late, but a
    # rider asking 15 min before 07:12 (at 06:57) knew none of that.
    reading(loaded, "central-90106", "07:25", "07:37", 25)
    reading(loaded, "central-90106", "07:35", "07:42", 30)
    row = line_state.backtest(loaded, leads=(15,))[0]
    assert row.mae["own"] == row.mae["zero"] == 30.0


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


def test_a_train_on_another_line_is_not_the_line_state(loaded, tmp_path):
    from conftest import harbour_csv

    load_timetable(loaded, read_timetable(harbour_csv(tmp_path), line="harbour"))
    # At CSMT, shared with the Harbour line: a late Harbour train going the same way
    # (down) says nothing about the main line, so 90103 has no line state.
    reading_at(loaded, "harbour-98001", "CSMT", "07:10", "07:22", 10)
    reading_at(loaded, "central-90103", "CSMT", "07:15", "07:31", 0)
    reading_at(loaded, "central-90103", "CSMT", "07:30", "07:41", 10)
    row = line_state.backtest(loaded, leads=(15,))[0]
    assert row.runs == 2 and row.with_line == 0
