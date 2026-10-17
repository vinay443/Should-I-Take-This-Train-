"""The baseline backtest: every reading vs each run once (sitt.models.baseline_check)."""

from datetime import datetime, timedelta

from sitt.models import baseline_check
from sitt.tz import IST

DAY = datetime(2026, 10, 6, tzinfo=IST)


def read(con, train_id, observed, expected, delay):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, actual_or_expected_time, "
        "time_kind, delay_minutes, source, event) "
        "VALUES (?, ?, 'KYN', ?, 'expected', ?, 'ntes', 'departure')",
        [observed, train_id, expected, delay],
    )


def run(con, train_id, day, final):
    """Read 40 min ahead as on time, then close to the train's time `final` min late."""
    due = DAY + timedelta(days=day, hours=7, minutes=12)
    read(con, train_id, due - timedelta(minutes=40), due, 0)
    expected = due + timedelta(minutes=final)
    read(con, train_id, expected - timedelta(minutes=2), expected, final)


def test_each_run_once_is_not_fooled_by_early_on_time_readings(loaded):
    run(loaded, "central-90106", 0, 10)  # up
    run(loaded, "central-90106", 1, 12)
    (overall, up) = baseline_check.backtest(loaded)
    # Day 1 from day 0: every reading gives median(0, 10) = 5, each run once gives 10.
    assert (overall.group, overall.runs) == ("all", 1) and up.group == "up"
    assert overall.mae_every_reading == 7 and overall.mae_each_run == 2
    assert overall.bias_every_reading == 7 and overall.bias_each_run == 2
    text = baseline_check.report([overall, up])
    assert "| Towards CSMT | 1 | 7.0 min | 2.0 min | +7.0 min | +2.0 min |" in text
    assert text.endswith(
        "Only 1 runs can be compared (200 needed): not enough to change the baseline. "
        "For now each run once is ahead."
    )


def test_nothing_to_compare(loaded):
    run(loaded, "central-90106", 0, 10)  # one day only
    assert baseline_check.backtest(loaded) == []
    assert baseline_check.report([]).startswith("No run has an earlier day")


def test_flagged_readings_are_left_out_as_the_recommender_leaves_them_out(loaded):
    run(loaded, "central-90106", 0, 10)
    run(loaded, "central-90106", 1, 12)
    # Flag day 0's early "on time" reading (sitt.dq would, for a suspect reading).
    (early,) = loaded.execute(
        "SELECT id FROM observations WHERE delay_minutes = 0 ORDER BY observed_at LIMIT 1"
    ).fetchone()
    loaded.execute(
        "INSERT INTO dq_flags (observation_id, flag, flagged_at) VALUES (?, 'test', now())",
        [early],
    )
    (overall, _up) = baseline_check.backtest(loaded)
    assert overall.mae_every_reading == 2  # only day 0's final reading, 10, is left


def test_runs_count_trains_on_days_not_stations(loaded):
    for station in ("KYN", "TNA"):
        for day, final in ((0, 10), (1, 12)):
            due = DAY + timedelta(days=day, hours=7, minutes=12)
            expected = due + timedelta(minutes=final)
            loaded.execute(
                "INSERT INTO observations (observed_at, train_id, station_code, "
                "actual_or_expected_time, time_kind, delay_minutes, source, event) "
                "VALUES (?, 'central-90106', ?, ?, 'expected', ?, 'ntes', 'departure')",
                [expected - timedelta(minutes=2), station, expected, final],
            )
    (overall, _up) = baseline_check.backtest(loaded)
    assert overall.runs == 1  # one train on one day, read at two stations
