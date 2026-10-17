"""Delay patterns from real readings (sitt.patterns)."""

import re
from datetime import datetime, timedelta

from sitt import patterns
from sitt.tz import IST

DAY = datetime(2026, 10, 6, tzinfo=IST)  # a Tuesday


def at(hhmm: str, day: int = 0) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return DAY + timedelta(days=day, hours=hours, minutes=minutes)


def run(con, train_id, due, delay, day=0, source="ntes", early_reading=True):
    """One run seen twice: 'on time' 40 min before, then close to its time with `delay`."""
    expected = at(due, day) + timedelta(minutes=delay)
    if early_reading:
        read(con, train_id, at(due, day) - timedelta(minutes=40), at(due, day), 0, source)
    read(con, train_id, expected - timedelta(minutes=2), expected, delay, source)


def read(con, train_id, observed, expected, delay, source="ntes"):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, actual_or_expected_time, "
        "time_kind, delay_minutes, source, event) "
        "VALUES (?, ?, 'KYN', ?, 'expected', ?, ?, 'departure')",
        [observed, train_id, expected, delay, source],
    )


def test_each_run_counts_once_with_its_final_delay(loaded):
    run(loaded, "central-90106", "07:12", 10)  # up, fast, 07 h
    run(loaded, "central-90104", "07:03", 0)  # up, slow, 07 h
    run(loaded, "central-90104", "07:03", 6, day=1)
    run(loaded, "central-90103", "07:21", 2)  # down, fast
    result = patterns.delay_patterns(loaded)

    assert result["runs"] == 4 and result["days"] == 2 and result["stations"] == ["KYN"]
    assert result["first_day"] == "2026-10-06" and result["last_day"] == "2026-10-07"
    up_7 = next(c for c in result["by_hour"] if c["direction"] == "up" and c["key"] == 7)
    # The early "on time" readings don't count: three runs, final delays 10, 0, 6.
    assert up_7["runs"] == 3 and up_7["median"] == 6.0
    assert round(up_7["share_late"], 2) == 0.67 and up_7["few"] is True
    bands = {(c["direction"], c["key"]): c for c in result["by_band"]}
    assert bands[("up", "am_peak")]["runs"] == 3 and bands[("down", "am_peak")]["runs"] == 1
    directions = {d["direction"]: d for d in result["by_direction"]}
    assert directions["up"]["runs"] == 3 and directions["up"]["median"] == 6.0
    assert directions["down"]["runs"] == 1 and directions["down"]["few"] is True
    assert [d["direction"] for d in result["by_direction"]] == ["up", "down"]
    types = {t["type"]: t for t in result["by_type"]}
    assert types["fast"]["runs"] == 2 and types["slow"]["runs"] == 2


def test_hours_use_the_scheduled_time_in_mumbai(loaded):
    # 90102 is due at 04:15 and ran 50 min late: it belongs to the 04 h, early-morning band.
    run(loaded, "central-90102", "04:15", 50)
    result = patterns.delay_patterns(loaded)
    assert [(c["key"], c["median"]) for c in result["by_hour"]] == [(4, 50.0)]
    assert [c["key"] for c in result["by_band"]] == ["early"]


def test_synthetic_long_distance_and_unfinished_runs_are_left_out(loaded):
    run(loaded, "central-90106", "07:12", 10, source="synthetic")
    run(loaded, "12618", "07:12", 30)  # not a suburban train
    # Only seen 40 minutes ahead: no reading close to its time, so no final delay.
    read(loaded, "central-90108", at("06:51"), at("07:31"), 3)
    result = patterns.delay_patterns(loaded)
    assert result["runs"] == 0 and result["by_hour"] == [] and result["first_day"] is None


def test_enough_runs_are_not_marked_few(loaded):
    for day in range(patterns.MIN_RUNS):
        run(loaded, "central-90110", "07:48", day, day=day, early_reading=False)
    (cell,) = patterns.delay_patterns(loaded)["by_hour"]
    assert cell["runs"] == patterns.MIN_RUNS and cell["few"] is False


def test_night_band_wraps_midnight():
    sql = patterns._band_sql()
    assert "WHEN hour >= 21 OR hour < 4 THEN 'night'" in sql
    covered = set()
    for _key, _label, start, end in patterns.BANDS:
        hours = range(start, end) if start < end else [*range(start, 24), *range(0, end)]
        assert not covered & set(hours)
        covered |= set(hours)
    assert covered == set(range(24))


def test_cli(tmp_path, capsys):
    from conftest import SAMPLE_CSV

    from sitt.db import init_db
    from sitt.ingest.timetable import load_timetable, read_timetable

    assert patterns.main(["--db", str(tmp_path / "missing.duckdb")]) == 2
    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    assert patterns.main(["--db", str(db)]) == 1
    assert "No real readings yet" in capsys.readouterr().out
    with init_db(db) as con:
        run(con, "central-90106", "07:12", 10)
        run(con, "central-90104", "07:03", 5)  # median 7.5 shows as 8, like the page
    assert patterns.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("2 trains seen at KYN, 2026-10-06 (1 day).")
    assert re.search(r"Morning peak \(07-11\)\s+8 min, 100%, 2\*\s+-\n", out)
    assert patterns.main(["--db", str(db), "--markdown"]) == 0
    assert "| Morning peak (07-11) | 8 min, 100%, 2* | - |" in capsys.readouterr().out


def test_report_rounds_like_the_page():
    # Math.round on the page: 12.5% -> 13%, 2.5 min -> 3 min (Python's round gives 12 and 2).
    assert patterns._percent(1 / 8) == "13%" and patterns._percent(5 / 8) == "63%"
    assert patterns._minutes(2.5) == "3 min" and patterns._minutes(-2.5) == "-2 min"


def test_a_train_after_midnight_belongs_to_the_day_before(loaded):
    # Due 23:40 on Tuesday and 00:30 early on Wednesday: one service day, as runs are.
    run(loaded, "central-90106", "23:40", 4)
    run(loaded, "central-90104", "00:30", 2, day=1)
    result = patterns.delay_patterns(loaded)
    assert result["days"] == 1
    assert result["first_day"] == result["last_day"] == "2026-10-06"


def test_weekends_and_holidays_are_told_apart_from_weekdays(loaded):
    run(loaded, "central-90106", "07:12", 10)  # Tuesday
    run(loaded, "central-90106", "07:12", 0, day=-4)  # Friday 2 Oct: Gandhi Jayanti
    run(loaded, "central-90106", "07:12", 0, day=-22)  # Monday 14 Sep: Ganesh Chaturthi
    run(loaded, "central-90106", "07:12", 0, day=4)  # Saturday
    run(loaded, "central-90104", "01:00", 0, day=6)  # 01:00 Monday: Sunday's service day
    run(loaded, "central-90104", "00:30", 8, day=4)  # 00:30 Saturday: Friday's
    kinds = {k["key"]: k for k in patterns.delay_patterns(loaded)["day_kinds"]}
    assert (kinds["weekday"]["days"], kinds["weekday"]["runs"]) == (2, 2)
    assert (kinds["weekend"]["days"], kinds["weekend"]["runs"]) == (4, 4)


def test_split_waits_for_two_days_of_each_kind(loaded):
    run(loaded, "central-90106", "07:12", 10)  # Tuesday
    run(loaded, "central-90106", "07:12", 6, day=1)  # Wednesday
    run(loaded, "central-90106", "07:12", 0, day=4)  # Saturday
    result = patterns.delay_patterns(loaded)
    assert result["split_ready"] is False and result["by_kind"] == {}
    assert result["split_min_days"] == patterns.SPLIT_MIN_DAYS == 2
    note = patterns.split_note(result)
    assert note == (
        "Weekdays and weekends are shown apart once trains have been seen on at least 2 days "
        "of each; so far 2 weekdays and 1 weekend day or holiday."
    )

    run(loaded, "central-90106", "07:12", 2, day=5)  # Sunday
    result = patterns.delay_patterns(loaded)
    assert result["split_ready"] is True
    weekday, weekend = result["by_kind"]["weekday"], result["by_kind"]["weekend"]
    assert (weekday["runs"], weekday["days"], weekday["by_direction"][0]["median"]) == (2, 2, 8.0)
    assert (weekend["runs"], weekend["days"], weekend["by_direction"][0]["median"]) == (2, 2, 1.0)
    assert [c["key"] for c in weekend["by_hour"]] == [7]
    # The all-days numbers are unchanged by the split.
    assert result["runs"] == 4 and result["by_direction"][0]["median"] == 4.0


def test_cli_days(tmp_path, capsys):
    from conftest import SAMPLE_CSV

    from sitt.db import init_db
    from sitt.ingest.timetable import load_timetable, read_timetable

    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        run(con, "central-90106", "07:12", 10)
        run(con, "central-90106", "07:12", 6, day=1)
        run(con, "central-90106", "07:12", 0, day=4)
    assert patterns.main(["--db", str(db)]) == 0
    assert (
        "on at least 2 days of each; so far 2 weekdays and 1 weekend day or holiday.\n"
        in capsys.readouterr().out
    )
    assert patterns.main(["--db", str(db), "--days", "weekend"]) == 1
    assert capsys.readouterr().out.startswith("Weekdays and weekends are shown apart once")

    with init_db(db) as con:
        run(con, "central-90106", "07:12", 2, day=5)
    assert patterns.main(["--db", str(db), "--days", "weekend"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(
        "Weekends & holidays only: 2 trains seen at KYN, 2026-10-10 to 2026-10-11 (2 days)."
    )
    assert re.search(r"Morning peak \(07-11\)\s+1 min, 0%, 2\*\s+-\n", out)
    assert patterns.main(["--db", str(db), "--days", "weekday"]) == 0
    assert re.search(r"Morning peak \(07-11\)\s+8 min, 100%, 2\*", capsys.readouterr().out)


def test_model_progress_counts_against_the_retrain_gates(loaded):
    run(loaded, "central-90106", "07:12", 10)
    run(loaded, "central-90104", "07:03", 2, day=1)
    model = patterns.model_progress(loaded)
    assert model["ready"] is False and model["in_use"] is None
    days = model["gates"][0]
    assert (days["name"], days["have"], days["need"], days["met"]) == (
        "days with observations",
        2,
        28,
        False,
    )
    note = patterns.model_note(model)
    assert note.startswith("Delay model: not enough real readings yet (days with observations")
    assert "2 of 28" in note and "5,000" in note

    loaded.execute(
        "INSERT INTO model_registry (version, created_at, status) "
        "VALUES ('real-20261101T000000Z', now(), 'promoted')"
    )
    model = patterns.model_progress(loaded)
    assert model["in_use"] == "real-20261101T000000Z"
    assert "real-20261101T000000Z, trained on real readings, is in use" in patterns.model_note(
        model
    )


def test_model_progress_on_synthetic_data_is_none(loaded):
    run(loaded, "central-90106", "07:12", 10, source="synthetic")
    assert patterns.model_progress(loaded) is None
    assert "synthetic" in patterns.model_note(None)


def test_cli_ends_with_the_model_progress(tmp_path, capsys):
    from conftest import SAMPLE_CSV

    from sitt.db import init_db
    from sitt.ingest.timetable import load_timetable, read_timetable

    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        run(con, "central-90106", "07:12", 10)
    assert patterns.main(["--db", str(db)]) == 0
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("Delay model: not enough real readings yet (days with observations 1")


def test_model_progress_without_a_model_registry(loaded):
    # A database made before sitt-retrain existed has no model_registry table.
    run(loaded, "central-90106", "07:12", 10)
    loaded.execute("DROP TABLE model_registry")
    model = patterns.model_progress(loaded)
    assert model["in_use"] is None and model["gates"][0]["have"] == 1
