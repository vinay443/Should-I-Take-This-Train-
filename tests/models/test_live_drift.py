"""How live readings drift before the train arrives (sitt.models.live_drift)."""

from datetime import datetime, timedelta

from sitt.models import live_drift
from sitt.tz import IST

DAY = datetime(2026, 10, 6, tzinfo=IST)


def at(hhmm: str) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return DAY + timedelta(hours=hours, minutes=minutes)


def reading(con, train_id, observed, expected, delay, event="departure", kind="expected"):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, actual_or_expected_time, "
        "time_kind, delay_minutes, source, event) VALUES (?, ?, 'KYN', ?, ?, ?, 'ntes', ?)",
        [at(observed), train_id, at(expected), kind, delay, event],
    )


def test_drift_by_lead_and_what_the_reading_said(loaded):
    # 90106 is due at Kalyan at 07:12. NTES says on time, then the delay appears.
    reading(loaded, "central-90106", "06:30", "07:12", 0)
    reading(loaded, "central-90106", "06:45", "07:12", 0)
    reading(loaded, "central-90106", "07:00", "07:21", 9)
    reading(loaded, "central-90106", "07:15", "07:22", 10)  # close to due: the "final" reading
    # A long-distance train, left out unless asked for.
    reading(loaded, "12618", "06:30", "08:00", 0)
    reading(loaded, "12618", "08:20", "08:30", 30)

    rows = live_drift.drift_table(loaded)
    as_tuples = [(r.said_on_time, r.lead_label, r.readings, r.median) for r in rows]
    # Leads are counted from the scheduled 07:12, not from when the late train came.
    assert as_tuples == [
        (True, "15-30 min", 1, 10.0),  # 06:45, 27 min before 07:12, said on time; ended 10 late
        (True, "30-60 min", 1, 10.0),  # 06:30, 42 min before
        (False, "0-15 min", 1, 1.0),  # 07:00, 12 min before, said 9 late; ended 10 late
    ]
    assert rows[0].share_5_later == 1.0 and rows[0].share_earlier == 0.0

    with_ld = live_drift.drift_table(loaded, suburban_only=False)
    assert sum(r.readings for r in with_ld) == 4
    assert "| on time | 30-60 min | 1 |" in live_drift.format_table(rows)


def test_readings_after_the_scheduled_time_have_their_own_row(loaded):
    # Still "on time" at 07:15, three minutes after 07:12: NTES hasn't caught up.
    reading(loaded, "central-90106", "07:15", "07:12", 0)
    reading(loaded, "central-90106", "07:20", "07:30", 18)
    rows = live_drift.drift_table(loaded)
    assert [(r.said_on_time, r.lead_label, r.median) for r in rows] == [
        (True, "after its time", 18.0)
    ]


def test_a_run_never_seen_close_to_its_time_is_left_out(loaded):
    reading(loaded, "central-90106", "06:30", "07:12", 0)
    reading(loaded, "central-90106", "06:45", "07:12", 3)  # 27 min before: not close enough
    assert live_drift.drift_table(loaded) == []


def test_cli(tmp_path, capsys):
    from conftest import SAMPLE_CSV

    from sitt.db import init_db
    from sitt.ingest.timetable import load_timetable, read_timetable

    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    assert live_drift.main(["--db", str(db)]) == 1
    assert "No pairs" in capsys.readouterr().out
    with init_db(db) as con:
        reading(con, "central-90106", "06:30", "07:12", 0)
        reading(con, "central-90106", "07:15", "07:22", 10)
    assert live_drift.main(["--db", str(db)]) == 0
    assert "| on time | 30-60 min | 1 |" in capsys.readouterr().out
    assert live_drift.main(["--db", str(tmp_path / "missing.duckdb")]) == 2
