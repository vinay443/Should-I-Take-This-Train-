"""Feature rows, checked by hand against a few invented observations.

Uses the sample timetable. 90104 is a slow up train (Kalyan 07:03, Kalwa 07:27, Kurla
07:58, Chinchpokli 08:14) and 90110 the next slow up train (Kalyan 07:48, Dombivli 07:56,
Kalwa 08:12, CSMT 09:09).
"""

from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from sitt.config import BlockSettings, load_block_settings
from sitt.models import features
from sitt.models.features import Target
from sitt.tz import IST

SUNDAY, MONDAY = date(2026, 9, 27), date(2026, 9, 28)

# (day, HH:MM poll, train number, station, delay)
READINGS = [
    (SUNDAY, "08:00", "90104", "CLA", 5),
    (SUNDAY, "08:15", "90104", "CHG", 9),
    (MONDAY, "07:15", "90104", "KOPR", 1),
    (MONDAY, "07:30", "90104", "KLVA", 3),
    (MONDAY, "08:00", "90104", "CLA", 2),
    (MONDAY, "08:00", "90110", "DI", 4),
    (MONDAY, "08:15", "90104", "CHG", 1),
    (MONDAY, "08:15", "90110", "KLVA", 3),
]


def at(day: date, hhmm: str) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes)


@pytest.fixture
def con(loaded):
    for day, hhmm, number, station, delay in READINGS:
        moment = at(day, hhmm).replace(tzinfo=IST)
        loaded.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
            "source, train_number, event, batch_id) VALUES (?, ?, ?, ?, 'test', ?, 'at', ?)",
            [moment, f"central-{number}", station, delay, number, f"{day}-{hhmm}"],
        )
    # Not usable as readings: a cancellation, and a station the train doesn't pass.
    loaded.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, source, event, cancelled)"
        " VALUES (?, 'central-90106', '', 'test', 'cancelled', true)",
        [at(MONDAY, "08:00").replace(tzinfo=IST)],
    )
    loaded.execute(
        "INSERT INTO blocks (block_id, block_date, line, from_station, to_station, start_time, "
        "end_time, tracks, direction, source) VALUES "
        "('b1', ?, 'central', 'MTN', 'MLND', '07:00', '09:00', 'slow', 'both', 'manual')",
        [MONDAY],
    )
    features.prepare(loaded)
    return loaded


def rows(columns, **where):
    """Rows of the feature columns matching every `where` value, as dicts."""
    n = len(columns["target_id"])
    out = []
    for i in range(n):
        row = {name: values[i] for name, values in columns.items()}
        if all(row[key] == value for key, value in where.items()):
            out.append(row)
    return out


def ts(day, hhmm):
    return np.datetime64(at(day, hhmm), "us")


def test_observations_get_their_service_day(con):
    assert con.execute("SELECT count(*) FROM obs").fetchone() == (8,)
    days = con.execute("SELECT service_day, count(*) FROM obs GROUP BY ALL ORDER BY 1").fetchall()
    assert days == [(SUNDAY, 2), (MONDAY, 6)]


def test_training_rows_one_per_earlier_reading_plus_one_cold(con):
    # Monday 90104: 4 readings -> 4 cold + 6 pairs. 90110: 2 -> 2 + 1. Sunday 90104: 2 -> 2 + 1.
    assert features.training_targets(con) == 16
    assert features.training_targets(con, start=MONDAY) == 13
    columns = features.build_features(con)
    assert len(columns["label"]) == 13
    assert set(features.FEATURES) <= set(columns)
    assert columns["has_prior"].sum() == 7


def test_features_of_a_row_with_a_live_reading(con):
    features.training_targets(con)
    columns = features.build_features(con)
    [row] = rows(
        columns,
        train_id="central-90104",
        station_code="CLA",
        cutoff=ts(MONDAY, "07:30"),
    )
    assert row["label"] == 2 and row["label_time"] == ts(MONDAY, "08:00")
    assert row["sched_time"] == ts(MONDAY, "07:58")
    assert (row["is_fast"], row["is_up"], row["is_stop"]) == (0, 1, 1)
    assert (row["hour"], row["weekday"], row["month"], row["sunday_schedule"]) == (7, 0, 9, 0)
    assert row["sched_minutes"] == 55  # 07:03 -> 07:58
    assert row["lead_minutes"] == 28  # 07:30 -> 07:58
    # The trip's own latest reading at the cutoff: Kalwa, 3 late, 31 scheduled minutes back.
    assert (row["has_prior"], row["prior_delay"], row["prior_lead_minutes"]) == (1, 3, 31)
    assert row["prior_points_back"] == 9
    # Only 90104 was running in the 07:30 batch, so it is the line and nothing is ahead.
    assert (row["line_median_delay"], row["line_count"]) == (3, 1)
    assert np.isnan(row["ahead_delay"])
    # Sunday's reading at Kurla is the history; Monday's own readings are not.
    assert (row["hist_delay"], row["hist_count"]) == (5, 1)
    assert row["megablock"] == 1  # Kurla is inside Matunga-Mulund, at 07:58, on the slow lines


def test_no_feature_looks_past_the_cutoff(con):
    features.training_targets(con)
    columns = features.build_features(con)
    for row in rows(columns):
        assert row["cutoff"] < row["label_time"]
    # The Kurla label at 08:00 seen from 07:15 knows only the 07:15 reading (1 late), not 07:30's.
    [early] = rows(
        columns, train_id="central-90104", station_code="CLA", cutoff=ts(MONDAY, "07:15")
    )
    assert early["prior_delay"] == 1
    # On the first day there is no history at all.
    sunday = rows(columns, service_day=np.datetime64(SUNDAY, "us"))
    assert len(sunday) == 3
    assert all(np.isnan(r["hist_delay"]) and r["hist_count"] == 0 for r in sunday)
    assert {r["sunday_schedule"] for r in sunday} == {1}


def test_train_ahead_and_line_state(con):
    features.training_targets(con)
    columns = features.build_features(con)
    # 90110 at Kalwa (08:15), seen from its 08:00 reading at Dombivli.
    [row] = rows(columns, train_id="central-90110", station_code="KLVA", cutoff=ts(MONDAY, "08:00"))
    assert row["prior_delay"] == 4
    # In the 08:00 batch 90104 was ahead of it at Kurla, 13 stations on, 2 late.
    assert (row["ahead_delay"], row["ahead_gap"]) == (2, 13)
    assert (row["line_median_delay"], row["line_count"]) == (3, 2)
    assert row["megablock"] == 0  # Kalwa is outside the blocked section

    # Its cold row: cutoff 07:38, ten minutes before it starts. The 07:30 batch is recent
    # enough, and 90104 (then at Kalwa) is the train ahead of Kalyan.
    [cold] = rows(columns, train_id="central-90110", station_code="KLVA", has_prior=0)
    assert cold["cutoff"] == ts(MONDAY, "07:38")
    assert np.isnan(cold["prior_delay"]) and np.isnan(cold["prior_lead_minutes"])
    assert (cold["ahead_delay"], cold["ahead_gap"]) == (3, 6)
    assert cold["lead_minutes"] == 34  # 07:38 -> 08:12


def test_prediction_targets(con):
    targets = [
        Target("central-90110", MONDAY, "CSMT", at(MONDAY, "08:05")),
        # An aware cutoff is converted to Mumbai time; 09:00 is 45 minutes after the last batch.
        Target("central-90110", MONDAY, "CSMT", at(MONDAY, "09:00").replace(tzinfo=IST)),
        Target("central-90110", MONDAY + timedelta(days=1), "CSMT", at(MONDAY, "23:00")),
        Target("central-90106", MONDAY, "THK", at(MONDAY, "07:00")),  # passes Thakurli
        Target("central-90113", MONDAY, "KYN", at(MONDAY, "07:00")),  # never reaches Kalyan
    ]
    features.set_targets(con, targets)
    columns = features.build_features(con)
    assert list(columns["target_id"]) == [0, 1, 2, 3]  # the last has no such station
    assert np.isnan(columns["label"]).all()

    first, stale, tomorrow, passing = (rows(columns, target_id=i)[0] for i in range(4))
    assert (first["prior_delay"], first["lead_minutes"]) == (4, 64)  # due 09:09
    assert (first["ahead_delay"], first["line_median_delay"]) == (2, 3)
    assert stale["prior_delay"] == 3  # the trip's 08:15 reading still counts
    assert np.isnan(stale["line_median_delay"]) and np.isnan(stale["ahead_delay"])
    assert (tomorrow["has_prior"], tomorrow["weekday"]) == (0, 1)
    assert (passing["is_stop"], passing["is_fast"]) == (0, 1)


def test_works_without_a_blocks_table(con):
    con.execute("DROP TABLE blocks")
    features.prepare(con)
    features.training_targets(con)
    assert features.build_features(con)["megablock"].sum() == 0


def test_a_block_without_times_covers_the_default_hours_not_the_whole_day(con):
    """A date-and-line block (all Yatri's page gives) counts only from 10:00 to 16:00."""
    con.execute("DELETE FROM blocks")
    con.execute(
        "INSERT INTO blocks (block_id, block_date, line, source) VALUES "
        "('b2', ?, 'central', 'yatri')",
        [MONDAY],
    )
    # 90104 is at Kurla at 07:58; 90113 runs Thane 08:55 -> ... in the sample, both before 10:00.
    morning = Target("central-90104", MONDAY, "CLA", at(MONDAY, "07:00"))

    def megablock(settings=None):
        features.prepare(con, settings)
        features.set_targets(con, [morning])
        return features.build_features(con)["megablock"][0]

    assert megablock() == 0  # 07:58 is outside 10:00-16:00
    assert megablock(BlockSettings(time(7, 0), time(9, 0))) == 1  # the hours are configurable
    assert megablock(BlockSettings(time(22, 0), time(8, 0))) == 1  # and may run past midnight

    # A block with its own times keeps them, whatever the default hours are.
    con.execute("UPDATE blocks SET start_time = '07:30', end_time = '08:30'")
    assert megablock() == 1
    assert megablock(BlockSettings(time(12, 0), time(13, 0))) == 1
    # A date-only block on another line or day never applies.
    con.execute("UPDATE blocks SET start_time = NULL, end_time = NULL, line = 'harbour'")
    assert megablock(BlockSettings(time(0, 0), time(23, 59))) == 0


def test_block_hours_from_the_environment(monkeypatch):
    assert load_block_settings() == BlockSettings(time(10, 0), time(16, 0))
    monkeypatch.setenv("SITT_BLOCK_DEFAULT_START", "9:30")
    monkeypatch.setenv("SITT_BLOCK_DEFAULT_END", "17:00")
    assert load_block_settings() == BlockSettings(time(9, 30), time(17, 0))
    monkeypatch.setenv("SITT_BLOCK_DEFAULT_END", "teatime")
    with pytest.raises(ValueError, match="SITT_BLOCK_DEFAULT_END"):
        load_block_settings()
