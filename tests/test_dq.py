"""Data-quality checks. Invented readings against the sample timetable; no network.

Sample train 90101 (slow, down) is due at Kalyan at 07:23 and leaves Dadar at 06:20.
"""

from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest
from conftest import SAMPLE_CSV

from sitt import dq, health
from sitt.config import DQSettings
from sitt.db import DatabaseBusyError, init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.models import features
from sitt.tz import IST

DAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=IST)


def ist(hhmm: str, day: date = DAY) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=IST)


def batch(moment: datetime, suffix: str = "aaaaaa") -> str:
    return f"{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}-{suffix}"


def add(
    con,
    seen: str,
    *,
    number: str = "90101",
    station: str = "KYN",
    event: str = "arrival",
    delay: float | None = 0.0,
    time: str | None = "07:23",
    matched: bool = True,
    source: str = "ntes",
    batch_id: str | None = None,
    cancelled: bool = False,
    less_accurate: bool = False,
    day: date = DAY,
) -> int:
    """Insert one reading taken at `seen` (HH:MM IST); returns its id."""
    observed = ist(seen, day)
    (row_id,) = con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, actual_or_expected_time, "
        "time_kind, delay_minutes, source, train_number, event, cancelled, less_accurate, "
        "batch_id) VALUES (?, ?, ?, ?, 'expected', ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            observed,
            f"central-{number}" if matched else number,
            station,
            ist(time, day) if time else None,
            delay,
            source,
            number,
            event,
            cancelled,
            less_accurate,
            batch_id or batch(observed),
        ],
    ).fetchone()
    return row_id


def flags(con, settings: DQSettings | None = None, now: datetime = NOW) -> dict[int, set[str]]:
    dq.find_flags(con, settings, now)
    out: dict[int, set[str]] = {}
    for row_id, flag in con.execute("SELECT id, flag FROM dq_found").fetchall():
        out.setdefault(row_id, set()).add(flag)
    return out


def test_clean_readings_have_no_flags(loaded):
    add(loaded, "06:45")
    add(loaded, "07:00", delay=2, time="07:25")
    add(loaded, "07:15", delay=4, time="07:27")
    assert flags(loaded) == {}
    report = dq.build_report(loaded, now=NOW)
    assert (report.rows, report.flagged_rows, report.flag_counts) == (3, 0, {})
    assert "3 real readings checked; 0 flagged (0.0%)" in dq.format_report(report)


def test_exact_and_near_duplicates(loaded):
    first = add(loaded, "07:00")
    exact = add(loaded, "07:00")  # the very same reading, stored twice
    near = add(loaded, "07:02", batch_id=batch(ist("07:02"), "bbbbbb"))  # a second collector
    later = add(loaded, "07:15")  # the next ordinary poll
    found = flags(loaded)
    assert found == {exact: {"exact_duplicate"}, near: {"near_duplicate"}}
    assert first not in found and later not in found


def test_implausible_delays_use_separate_bounds_for_long_distance_trains(loaded):
    early = add(loaded, "06:50", delay=-25, time="06:58")
    late = add(loaded, "07:00", number="90103", delay=200, time="10:41")
    express_late = add(loaded, "07:00", number="12127", matched=False, delay=411, time="07:10")
    express_absurd = add(loaded, "07:00", number="12128", matched=False, delay=2000, time="07:10")
    found = flags(loaded)
    assert "implausible_delay" in found[early] and "implausible_delay" in found[late]
    assert express_late not in found  # nearly seven hours late is believable for an express
    assert found[express_absurd] == {"implausible_delay"}
    loose = DQSettings(max_early_minutes=30, max_delay_minutes=300)
    assert "implausible_delay" not in flags(loaded, loose).get(early, set())


def test_delay_jumps_between_consecutive_readings(loaded):
    add(loaded, "06:30", delay=2, time="07:25")
    grew = add(loaded, "06:45", delay=14, time="07:37")  # +12 in 15 min: a train can stall
    jumped = add(loaded, "07:00", delay=60, time="08:23")  # +46 in 15 min: it can't lose that
    recovered = add(loaded, "07:15", delay=5, time="07:28")  # -55 in 15 min: nor win it back
    add(loaded, "07:00", number="90103", delay=40, time="08:01")  # another train: no neighbour
    found = flags(loaded, DQSettings(schedule_mismatch_minutes=999, max_delay_minutes=999))
    assert grew not in found
    assert found[jumped] == {"delay_jump"} and found[recovered] == {"delay_jump"}
    next_day = add(loaded, "06:30", delay=50, time="08:13", day=DAY + timedelta(days=1))
    assert next_day not in flags(
        loaded, DQSettings(schedule_mismatch_minutes=999), NOW + timedelta(days=1)
    )


def test_schedule_mismatch_and_far_from_schedule(loaded):
    # The source says "on time" at 09:47, but our timetable has this train at 07:23.
    wrong = add(loaded, "09:30", delay=0, time="09:47")
    # Read at noon for a train due at 07:23 and 2 minutes late: nearly five hours off.
    stale = add(loaded, "12:00", delay=2, time="07:25")
    drift = add(loaded, "07:00", number="90103", delay=0, time="07:19")  # 2 min of drift only
    found = flags(loaded)
    assert found[wrong] == {"schedule_mismatch"}
    assert found[stale] == {"far_from_schedule"}
    assert drift not in found
    report = dq.build_report(loaded, now=NOW)
    assert report.schedule_drift == [("90103", "KYN", -2.0)]
    assert "90103 at KYN: -2 min" in dq.format_report(report)


def test_schedule_checks_work_across_midnight(loaded):
    # A reading taken just after midnight of a train due just before it, and the reverse.
    loaded.execute(
        "UPDATE scheduled_stops SET scheduled_arrival = '23:55' "
        "WHERE train_id = 'central-90101' AND station_code = 'KYN'"
    )
    tomorrow = DAY + timedelta(days=1)
    after = add(loaded, "00:05", delay=12, time="00:07", day=tomorrow)
    before = add(loaded, "23:40", delay=12, time="00:07")
    loaded.execute(
        "UPDATE observations SET actual_or_expected_time = ? WHERE id = ?",
        [ist("00:07", tomorrow), before],
    )
    assert flags(loaded, now=NOW + timedelta(days=1)) == {}
    assert after and before


def test_timestamp_sanity(loaded):
    future = add(loaded, "13:00", time=None, delay=None, event="unknown", station="")
    # Stamped as if 07:00 UTC were 07:00 IST: 5.5 hours from its batch's own time.
    shifted = add(loaded, "07:00", batch_id=batch(ist("12:30")))
    fine = add(loaded, "07:00", number="90103", time="07:21")
    found = flags(loaded)
    assert found[future] == {"future_timestamp"}
    assert found[shifted] == {"batch_time_mismatch"}
    assert fine not in found
    dq.build_report(loaded, now=NOW)
    (detail,) = loaded.execute(
        "SELECT detail FROM dq_found WHERE flag = 'batch_time_mismatch'"
    ).fetchone()
    assert detail == "stamped -330.0 min from its batch"


def test_unmatched_trains_stations_and_source_rates(loaded):
    add(loaded, "07:00")
    add(loaded, "07:00", number="95999", matched=False, time="07:05")  # suburban, not in timetable
    add(loaded, "07:00", number="12127", matched=False, time="07:05")
    add(loaded, "07:15", number="12127", matched=False, time="07:05")
    add(
        loaded,
        "07:00",
        number="97001",
        matched=False,
        station="LONAVLA",
        event="at",
        source="mobond",
        time=None,
        less_accurate=True,
    )
    add(
        loaded,
        "07:00",
        number="97002",
        matched=False,
        station="",
        event="cancelled",
        source="mobond",
        time=None,
        delay=None,
        cancelled=True,
    )
    add(loaded, "07:00", number="90103", source="synthetic", time="07:21")
    report = dq.build_report(loaded, now=NOW)
    assert report.rows == 6  # the synthetic reading is left out
    assert report.unmatched_suburban == [("95999", 1), ("97001", 1), ("97002", 1)]
    assert report.unmatched_other == [("12127", 2)]
    assert report.unmatched_stations == [("LONAVLA", 1)]
    rates = {r.source: r for r in report.sources}
    assert (rates["mobond"].rows, rates["mobond"].cancelled, rates["mobond"].less_accurate) == (
        2, 1, 1,
    )  # fmt: skip
    assert (rates["ntes"].rows, rates["ntes"].cancelled) == (4, 0)
    text = dq.format_report(report)
    assert "Suburban train numbers (95xxx-99xxx): 3: 95999 (1), 97001 (1), 97002 (1)" in text
    assert "mobond: 2 readings; cancelled 50.0%; less accurate 50.0%" in text
    assert "Station codes: 1: LONAVLA (1)" in text
    assert dq.headline_lines(report)[1] == (
        "not in the timetable: 3 suburban train number(s), 1 long-distance, 1 station code(s)"
    )


def test_the_report_window_limits_what_is_counted_but_not_what_is_compared(loaded):
    add(loaded, "06:30", delay=2, time="07:25")
    jumped = add(loaded, "07:00", delay=60, time="08:23")
    report = dq.build_report(
        loaded, ist("06:45"), NOW, DQSettings(schedule_mismatch_minutes=999), NOW
    )
    assert report.rows == 1 and report.flag_counts == {"delay_jump": 1}
    assert jumped  # its earlier neighbour is outside the window but still used


def test_nothing_is_deleted_and_flags_are_stored_idempotently(loaded):
    add(loaded, "07:00")
    add(loaded, "07:00")
    add(loaded, "09:30", delay=0, time="09:47")
    before = loaded.execute("SELECT count(*), sum(id) FROM observations").fetchone()
    assert dq.refresh_flags(loaded, now=NOW) == 2
    assert dq.refresh_flags(loaded, now=NOW) == 2
    assert loaded.execute("SELECT count(*), sum(id) FROM observations").fetchone() == before
    stored = loaded.execute("SELECT flag, detail FROM dq_flags ORDER BY flag").fetchall()
    assert [flag for flag, _ in stored] == ["exact_duplicate", "schedule_mismatch"]
    assert stored[1][1] == "source says 0.0 min late; our timetable implies 144"
    # A flag that no longer applies goes away when flags are recomputed.
    loose = DQSettings(schedule_mismatch_minutes=999, far_from_schedule_minutes=999)
    assert dq.refresh_flags(loaded, loose, NOW) == 1


def test_the_feature_builder_leaves_flagged_readings_out_by_default(loaded):
    for seen, station, delay in (("06:25", "DR", 3), ("06:40", "GC", 4), ("07:05", "MBQ", 5)):
        loaded.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
            "source, train_number, event, batch_id) VALUES (?, 'central-90101', ?, ?, 'test', "
            "'90101', 'at', ?)",
            [ist(seen), station, delay, batch(ist(seen))],
        )
    bad = add(loaded, "07:10", delay=200, time="10:43", source="test")
    features.prepare(loaded)
    assert loaded.execute("SELECT count(*) FROM obs").fetchone() == (4,)  # nothing flagged yet

    dq.find_flags(loaded, now=NOW, include_synthetic=True)
    dq.write_flags(loaded, NOW)
    assert loaded.execute("SELECT observation_id FROM dq_flags").fetchall() == [(bad,)]
    features.prepare(loaded)
    assert loaded.execute("SELECT count(*), max(delay_minutes) FROM obs").fetchone() == (3, 5.0)
    assert features.training_targets(loaded) == 6  # 3 cold rows + 3 pairs

    features.prepare(loaded, exclude_flagged=False)
    assert loaded.execute("SELECT count(*), max(delay_minutes) FROM obs").fetchone() == (4, 200.0)


def test_features_work_on_a_database_without_the_flags_table(tmp_path):
    with duckdb.connect(str(tmp_path / "old.duckdb")) as con:
        from sitt.db.database import SCHEMA_SQL

        con.execute(SCHEMA_SQL)
        con.execute("DROP TABLE dq_flags")
        load_timetable(con, read_timetable(SAMPLE_CSV))
        add(con, "07:00", source="test")
        features.prepare(con)
        assert con.execute("SELECT count(*) FROM obs").fetchone() == (1,)


def test_headline_plugs_into_the_health_report(loaded):
    add(loaded, "07:00")
    add(loaded, "07:00")
    report = health.build_report(loaded, ist("00:00"), NOW, now=NOW, data_quality=dq.headline)
    assert report.data_quality[0] == "1 of 2 readings flagged (50.0%): exact_duplicate 1"
    assert "Data quality:" in health.format_report(report)

    def broken(con, start, end):
        raise ValueError("boom")

    survived = health.build_report(loaded, ist("00:00"), NOW, now=NOW, data_quality=broken)
    assert survived.data_quality == ["could not be checked (ValueError); run `sitt-dq` to see why"]


# --- the CLI ---


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        add(con, "07:00")
        add(con, "07:00")
        add(con, "09:30", delay=0, time="09:47")
    return path


def test_cli_reports_read_only_and_writes_markdown(db, capsys):
    assert dq.main(["--db", str(db), "--markdown"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "3 real readings checked; 2 flagged (66.7%)" in out
    assert "exact_duplicate: 1" in out and "schedule_mismatch: 1" in out
    markdown = (db.parent / "scratch" / "dq-report.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Observation data quality: all readings up to 05 Oct 2026 12:00")
    assert "## Flags" in markdown and "- exact_duplicate: 1" in markdown
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM dq_flags").fetchone() == (0,)


def test_cli_hours_and_write_flags(db, capsys):
    assert dq.main(["--db", str(db), "--hours", "3", "--write-flags"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "1 real readings checked; 1 flagged" in out  # only the 09:30 reading is in the window
    assert "Stored 2 flag(s) in dq_flags." in out  # but flags always cover everything
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM dq_flags").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM observations").fetchone() == (3,)


def test_cli_handles_a_busy_or_missing_database(db, monkeypatch, capsys):
    assert dq.main(["--db", str(db.parent / "nope.duckdb")]) == 2
    assert "No database at" in capsys.readouterr().err

    def busy(*args, **kwargs):
        raise DatabaseBusyError("sitt.duckdb is in use by another process")

    monkeypatch.setattr(dq, "open_with_retry", busy)
    assert dq.main(["--db", str(db)]) == 2
    assert "in use by another process" in capsys.readouterr().err
