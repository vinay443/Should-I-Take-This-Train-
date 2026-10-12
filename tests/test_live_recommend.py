"""Live readings in recommendations: a recent reading of a train replaces its predicted delay."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from sitt.bot import formatting
from sitt.config import RecommendSettings, load_recommend_settings
from sitt.recommend import explain, recommend
from sitt.tz import IST

# Sample timetable (invented): 90104 slow KYN 07:03 -> TNA 07:32 -> CSMT 08:24,
# 90106 fast KYN 07:12 -> TNA 07:34 -> DR 08:05 -> CSMT 08:19, 90108 fast KYN 07:31.
MONDAY_7 = datetime(2026, 9, 28, 7, 0)


def _reading(con, when: datetime, number: str, station: str, delay, source="ntes"):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source, "
        "train_number, event, batch_id) VALUES (?, ?, ?, ?, ?, ?, 'at', ?)",
        [when.replace(tzinfo=IST), f"central-{number}", station, delay, source, number,
         f"{when:%Y%m%dT%H%M}"],
    )  # fmt: skip


def _by_number(rec):
    return {option.trip.number: option for option in rec.options}


def test_a_fresh_reading_replaces_the_timetable_time(loaded):
    _reading(loaded, MONDAY_7 - timedelta(minutes=5), "90106", "KYN", 9)
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert rec.level == "timetable"  # no history: the level is unchanged
    fast = _by_number(rec)["90106"]
    assert fast.live is not None and fast.live.delay_minutes == 9
    assert (fast.departure_delay, fast.arrival_delay) == (9, 9)
    assert fast.predicted_arrival == datetime(2026, 9, 28, 8, 28)
    assert all(o.live is None for o in rec.options if o.trip.number != "90106")
    # 08:28 is now later than the 07:03 slow's 08:24, so the slow is the pick.
    assert rec.chosen.trip.number == "90104"
    assert any(note.startswith("Live readings used for 1 train ") for note in rec.notes)

    reply = formatting.format_recommendation(rec)
    assert "(9 min late) · live" in reply
    assert "except trains marked live" in formatting.prediction_footer(rec)
    why = explain(rec)
    assert "live: 9 min late at 06:55, at KYN (ntes)" in why
    assert "except those with a live reading" in why


def test_the_reason_names_the_live_reading_of_the_pick(loaded):
    _reading(loaded, MONDAY_7 - timedelta(minutes=2), "90106", "KYN", 2)
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert rec.chosen.trip.number == "90106"
    assert rec.reason.startswith(
        "Wait for the 07:12 fast: arrives 08:21 (live: 2 min late at 06:58)"
    )


def test_stale_future_and_other_day_readings_are_ignored(loaded):
    _reading(loaded, MONDAY_7 - timedelta(minutes=31), "90106", "KYN", 9)  # too old
    _reading(loaded, MONDAY_7 + timedelta(minutes=1), "90108", "KYN", 9)  # after "now"
    _reading(loaded, MONDAY_7 - timedelta(days=1, minutes=5), "90104", "KYN", 9)  # yesterday
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert all(o.live is None for o in rec.options)
    assert not any("Live readings" in note for note in rec.notes)


def test_the_age_limit_comes_from_settings(loaded, monkeypatch):
    _reading(loaded, MONDAY_7 - timedelta(minutes=20), "90106", "KYN", 9)
    assert _by_number(recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None))["90106"].live
    short = replace(RecommendSettings(), live_max_age_minutes=10)
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, short, model_dir=None)
    assert _by_number(rec)["90106"].live is None
    off = replace(RecommendSettings(), live_max_age_minutes=0)
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, off, model_dir=None)
    assert _by_number(rec)["90106"].live is None

    assert load_recommend_settings().live_max_age_minutes == 30
    monkeypatch.setenv("SITT_LIVE_MAX_AGE_MINUTES", "12")
    assert load_recommend_settings().live_max_age_minutes == 12
    monkeypatch.setenv("SITT_LIVE_MAX_AGE_MINUTES", "-5")
    with pytest.raises(ValueError, match="SITT_LIVE_MAX_AGE_MINUTES"):
        load_recommend_settings()


def test_a_reading_before_the_boarding_station_counts_one_after_it_does_not(loaded):
    at_thane = MONDAY_7.replace(hour=7, minute=20)
    _reading(loaded, at_thane - timedelta(minutes=5), "90106", "KYN", 6)  # upstream of TNA
    _reading(loaded, at_thane - timedelta(minutes=5), "90104", "DR", 30)  # downstream of TNA
    rec = recommend(loaded, "TNA", "CSMT", at_thane, model_dir=None)
    by_number = _by_number(rec)
    assert by_number["90106"].live.station_code == "KYN"
    assert by_number["90106"].departure_delay == 6
    assert by_number["90104"].live is None and by_number["90104"].departure_delay == 0


def test_live_keeps_the_predicted_change_en_route(loaded):
    # History: 90106 on time at Kalyan and 12-16 min late into CSMT (median 14).
    for back, delay in enumerate([12, 13, 14, 15, 16, 14], start=1):
        day = MONDAY_7 - timedelta(days=back)
        _reading(loaded, day.replace(hour=8, minute=30), "90106", "CSMT", delay)
        _reading(loaded, day.replace(hour=7, minute=12), "90106", "KYN", 0)
    baseline = _by_number(recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None))["90106"]
    assert (baseline.departure_delay, baseline.arrival_delay) == (0, 14)

    _reading(loaded, MONDAY_7 - timedelta(minutes=3), "90106", "KYN", 5)
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert rec.level == "baseline"
    fast = _by_number(rec)["90106"]
    assert (fast.departure_delay, fast.arrival_delay) == (5, 19)
    assert (fast.arrival_low, fast.arrival_high) == (
        baseline.arrival_low + 5,
        baseline.arrival_high + 5,
    )
    assert fast.arrival_margin == baseline.arrival_margin
    assert "Trains marked live" in formatting.prediction_footer(rec)


def test_a_late_train_whose_time_has_passed_can_still_be_caught(loaded):
    at = MONDAY_7.replace(hour=7, minute=20)
    # The 07:12 fast is running 20 min late: it leaves Kalyan at about 07:32.
    _reading(loaded, at - timedelta(minutes=4), "90106", "KYN", 20)
    rec = recommend(loaded, "KYN", "CSMT", at, model_dir=None)
    numbers = [o.trip.number for o in rec.options]
    assert numbers[0] == "90106"
    late = rec.options[0]
    assert late.predicted_departure == datetime(2026, 9, 28, 7, 32)
    assert "Running late, so still to leave: the 07:12 fast." in rec.notes
    # The 07:31 fast behind it now follows a late train, which the crowding rule sees.
    assert "the train before it is 20 min late" in _by_number(rec)["90108"].crowding.reason


def test_a_late_train_that_has_already_gone_is_not_a_candidate(loaded):
    at = MONDAY_7.replace(hour=7, minute=20)
    _reading(loaded, at - timedelta(minutes=4), "90106", "KYN", 5)  # left at about 07:17
    rec = recommend(loaded, "KYN", "CSMT", at, model_dir=None)
    today = [o.trip.number for o in rec.options if o.trip.departure.date() == at.date()]
    assert "90106" not in today
    assert not any(note.startswith("Running late") for note in rec.notes)


def test_a_synthetic_live_reading_flags_the_recommendation(loaded):
    _reading(loaded, MONDAY_7 - timedelta(minutes=5), "90106", "KYN", 9, source="synthetic")
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert rec.synthetic
    assert "SYNTHETIC" in formatting.prediction_footer(rec)


def test_an_on_time_reading_never_overrides(loaded):
    # NTES says "on time" until a train is about to arrive (docs/live-drift.md).
    _reading(loaded, MONDAY_7 - timedelta(minutes=5), "90106", "KYN", 0)  # 17 min before 07:12
    _reading(loaded, MONDAY_7 - timedelta(minutes=2), "90104", "KYN", 0)  # 5 min before 07:03
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    by_number = _by_number(rec)
    assert by_number["90106"].live is None and by_number["90104"].live is None
    assert any(note.startswith("NTES shows 2 trains as on time for now.") for note in rec.notes)


def test_a_delay_read_far_ahead_is_still_used(loaded):
    _reading(loaded, MONDAY_7 - timedelta(minutes=5), "90108", "KYN", 4)  # 36 min before 07:31
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert _by_number(rec)["90108"].live.delay_minutes == 4
    assert "delays read ahead of time usually grow" in explain(rec)


# --- found by review ---


def test_asked_mid_minute_a_train_is_listed_once(loaded):
    _reading(loaded, MONDAY_7.replace(minute=10), "90106", "KYN", 3)
    asked = MONDAY_7.replace(minute=12, second=30)  # the bot asks with seconds
    rec = recommend(loaded, "KYN", "CSMT", asked, model_dir=None)
    today = [(o.trip.number, o.trip.departure) for o in rec.options]
    assert len(today) == len(set(today))
    assert rec.options[0].trip.number == "90106"
    assert not any(note.startswith("Running late") for note in rec.notes)
    assert any(note.startswith("Live readings used for 1 train ") for note in rec.notes)


def test_without_readings_the_first_train_is_not_its_own_train_ahead(loaded):
    loaded.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, source, train_number, "
        "event, cancelled) VALUES (?, 'central-90106', '', 'ntes', '90106', 'cancelled', true)",
        [MONDAY_7.replace(tzinfo=IST)],
    )
    asked = MONDAY_7.replace(minute=12, second=30)
    rec = recommend(loaded, "KYN", "CSMT", asked, model_dir=None)
    first = rec.options[0]
    assert first.trip.number == "90106" and first.cancelled
    assert "the train before it is cancelled" not in first.crowding.reason


def test_an_early_reading_never_moves_a_departure_earlier(loaded):
    for minutes_before, delay in ((5, -3), (1, -2)):  # far ahead, and just before
        _reading(loaded, MONDAY_7.replace(minute=12) - timedelta(minutes=minutes_before + 12),
                 "90106", "KYN", delay)  # fmt: skip
        rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
        fast = _by_number(rec)["90106"]
        assert fast.live is None and fast.departure_delay == 0
        assert fast.predicted_departure == datetime(2026, 9, 28, 7, 12)


def test_explain_at_timetable_level_doesnt_deny_todays_readings(loaded):
    _reading(loaded, MONDAY_7 - timedelta(minutes=2), "90104", "KYN", 0)
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert rec.level == "timetable"
    assert "no readings of these trains from earlier days" in explain(rec)
    assert "no past observations" not in explain(rec)
