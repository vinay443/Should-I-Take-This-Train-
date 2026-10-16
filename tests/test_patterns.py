"""Delay patterns from real readings (sitt.patterns)."""

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
