"""The "take this or wait?" rule, and which level of prediction a recommendation uses."""

from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest
from conftest import SAMPLE_CSV

from sitt.bot import formatting
from sitt.config import RecommendSettings, load_recommend_settings
from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.models.crowding import CROWD_LABELS, CrowdingEstimate
from sitt.models.delay import TrainConfig, train
from sitt.recommend import Option, choose, explain, recommend
from sitt.synth import build_database
from sitt.timetable import ScheduledTrip
from sitt.tz import IST

DAY = datetime(2026, 10, 5)  # a Monday
SETTINGS = RecommendSettings()


def clock(hhmm: str) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return DAY.replace(hour=hours % 24, minute=minutes) + timedelta(days=hours // 24)


def option(
    departs: str,
    arrives: str,
    crowd: int = 3,
    late: float = 0,
    late_arriving: float | None = None,
    kind: str = "fast",
    cancelled: bool = False,
    ladies: bool = False,
    margin: float | None = None,
) -> Option:
    """A candidate train. `late` is its predicted delay at the boarding station."""
    trip = ScheduledTrip(
        train_id=f"t-{departs}",
        number=departs.replace(":", ""),
        label="CSMT",
        train_type=kind,
        direction="up",
        departure=clock(departs),
        arrival=clock(arrives),
        is_ladies_special=ladies,
    )
    arrival_delay = late if late_arriving is None else late_arriving
    return Option(
        trip=trip,
        departure_delay=late,
        arrival_delay=arrival_delay,
        arrival_low=None if margin is None else arrival_delay - margin,
        arrival_high=None if margin is None else arrival_delay + margin,
        cancelled=cancelled,
        crowding=CrowdingEstimate(crowd, CROWD_LABELS[crowd], "test", float(crowd)),
    )


def pick(options, settings=SETTINGS):
    index, reason, rule = choose(options, settings)
    return (None if index is None else options[index].trip.number), rule, reason


def test_earliest_arrival_wins():
    slow_now = option("08:10", "09:40", kind="slow")
    fast_soon = option("08:12", "08:58", crowd=4, margin=4)
    fast_later = option("08:27", "09:13", crowd=4)
    number, rule, reason = pick([slow_now, fast_soon, fast_later])
    assert (number, rule) == ("0812", "earliest")
    # "Wait", because the 08:10 leaves first; the reason gives the range and the alternative.
    assert reason == (
        "Wait for the 08:12 fast: arrives 08:58 ±4 min, likely packed, "
        "but the next best (08:27 fast) arrives 15 min later."
    )


def test_take_when_the_pick_is_the_next_train_and_wait_when_it_is_not():
    assert pick([option("08:12", "08:58"), option("08:20", "09:10")])[2].startswith(
        "Take the 08:12 fast: arrives 08:58, earliest arrival, likely standing."
    )
    assert pick([option("08:12", "09:40"), option("08:20", "09:10")])[2].startswith(
        "Wait for the 08:20 fast"
    )


def test_ties_go_to_the_train_that_leaves_first():
    assert pick([option("08:12", "09:00"), option("08:15", "09:00")])[0] == "0812"
    # Predicted, not scheduled, arrival decides: the 08:12 is due first but runs 10 late.
    late_first = [option("08:12", "09:00", late=10), option("08:15", "09:05")]
    assert pick(late_first)[0] == "0815"


def test_nothing_to_recommend():
    assert pick([]) == (None, "none", "No trains found in the timetable for the next day.")
    number, rule, reason = pick(
        [option("08:12", "09:00", cancelled=True), option("08:20", "09:10", cancelled=True)]
    )
    assert (number, rule) == (None, "none") and "reported cancelled" in reason


def test_a_single_train():
    number, rule, reason = pick([option("08:12", "09:00", crowd=5)])
    assert (number, rule) == ("0812", "only")
    assert reason == (
        "Take the 08:12 fast: arrives 09:00, it is the only train running that I can see."
    )


def test_cancelled_trains_are_skipped_and_mentioned():
    options = [option("08:12", "08:58", cancelled=True), option("08:27", "09:13", crowd=4)]
    number, rule, reason = pick(options)
    assert (number, rule) == ("0827", "only")
    # "Take", not "Wait": no earlier train is running.
    assert reason.startswith("Take the 08:27 fast: arrives 09:13, the 08:12 fast is reported")
    # A cancelled train further down the list isn't mentioned.
    options = [option("08:12", "08:58"), option("08:27", "09:13", cancelled=True)]
    assert "cancelled" not in pick(options)[2]


# --- Waiting for a less crowded train ---------------------------------------------------


@pytest.mark.parametrize(
    ("later_arrives", "later_crowd", "expected"),
    [
        ("09:02", 2, "0820"),  # 4 minutes later and two levels emptier: wait
        ("09:03", 3, "0820"),  # exactly at the 5-minute limit, one level emptier: wait
        ("09:04", 3, "0812"),  # 6 minutes: too long
        ("09:02", 4, "0812"),  # no emptier: don't wait
        ("09:02", 5, "0812"),  # fuller
        ("08:58", 3, "0820"),  # arrives at the same time and emptier
    ],
)
def test_wait_for_a_less_crowded_train_only_if_it_is_nearly_as_quick(
    later_arrives, later_crowd, expected
):
    options = [
        option("08:12", "08:58", crowd=4),
        option("08:20", later_arrives, crowd=later_crowd, kind="slow"),
    ]
    assert pick(options)[0] == expected


def test_crowding_reason_names_both_trains():
    options = [option("08:12", "08:58", crowd=4), option("08:20", "09:02", crowd=2, kind="slow")]
    number, rule, reason = pick(options)
    assert (number, rule) == ("0820", "crowding")
    assert reason == (
        "Wait for the 08:20 slow: arrives 09:02, 4 min after the 08:12 fast, "
        "and likely seats free rather than packed."
    )


def test_an_emptier_train_that_leaves_first_is_taken_not_waited_for():
    options = [option("08:10", "09:02", crowd=2, kind="slow"), option("08:12", "08:58", crowd=4)]
    number, rule, reason = pick(options)
    assert (number, rule) == ("0810", "crowding")
    assert reason.startswith("Take the 08:10 slow: arrives 09:02, 4 min after the 08:12 fast")


def test_among_several_emptier_trains_the_emptiest_then_the_earliest():
    options = [
        option("08:12", "08:58", crowd=5),
        option("08:14", "08:59", crowd=4),
        option("08:16", "09:02", crowd=2),
        option("08:18", "09:01", crowd=2),
        option("08:20", "09:03", crowd=3),
    ]
    assert pick(options)[0] == "0818"


def test_crowding_thresholds_come_from_settings():
    options = [option("08:12", "08:58", crowd=4), option("08:20", "09:06", crowd=3)]
    assert pick(options)[0] == "0812"
    assert pick(options, RecommendSettings(wait_max_extra_minutes=10))[0] == "0820"
    picky = RecommendSettings(wait_max_extra_minutes=10, crowd_gain_levels=2)
    assert pick(options, picky)[0] == "0812"
    never = RecommendSettings(wait_max_extra_minutes=0)
    assert (
        pick([option("08:12", "08:58", crowd=4), option("08:20", "08:59", crowd=1)], never)[0]
        == "0812"
    )


# --- Very late trains ---------------------------------------------------------------------


def test_a_very_late_train_is_passed_over_if_another_arrives_soon_after():
    # The 08:12 runs 20 late but would still arrive first (09:10 against 09:16).
    options = [option("08:12", "08:50", late=20), option("08:25", "09:16")]
    number, rule, reason = pick(options)
    assert (number, rule) == ("0825", "very_late")
    assert reason == (
        "Wait for the 08:25 fast: arrives 09:16, the 08:12 fast is running about 20 min late, "
        "so its arrival is less certain."
    )


def test_a_very_late_train_is_still_taken_when_the_alternative_is_much_later():
    options = [option("08:12", "08:50", late=20), option("08:25", "09:25")]
    assert pick(options)[:2] == ("0812", "earliest")  # 15 minutes later is beyond the slack
    exactly = [option("08:12", "08:50", late=20), option("08:25", "09:20")]
    assert pick(exactly)[0] == "0825"  # 10 minutes: at the limit


def test_fourteen_minutes_late_is_not_very_late():
    options = [option("08:12", "08:50", late=14), option("08:25", "09:08")]
    assert pick(options)[:2] == ("0812", "earliest")
    assert pick(options, RecommendSettings(very_late_minutes=10))[1] == "very_late"


def test_when_every_train_is_very_late_take_the_earliest():
    options = [option("08:12", "08:50", late=20), option("08:25", "09:00", late=18)]
    assert pick(options)[:2] == ("0812", "earliest")


def test_crowding_never_switches_to_a_very_late_train():
    options = [option("08:12", "09:00", crowd=4), option("08:14", "08:44", crowd=1, late=20)]
    # The 08:14 would arrive at 09:04, within 5 minutes and far emptier, but it is very late.
    assert pick(options)[0] == "0812"


def test_a_train_late_only_at_the_destination_is_not_very_late():
    options = [option("08:12", "08:50", late=0, late_arriving=20), option("08:25", "09:16")]
    assert pick(options)[:2] == ("0812", "earliest")  # predicted to arrive 09:10


# --- Ladies' specials ---------------------------------------------------------------------


def test_ladies_specials_are_not_recommended_unless_allowed():
    options = [option("08:09", "08:50", crowd=2, ladies=True), option("08:14", "09:00", crowd=4)]
    assert pick(options)[0] == "0814"
    assert pick(options)[2].startswith("Take the 08:14")  # nothing it could board leaves earlier
    assert pick(options, RecommendSettings(ladies_special_ok=True))[0] == "0809"
    only = [option("08:09", "08:50", ladies=True)]
    assert pick(only) == (None, "none", "The only trains I can see running are ladies' specials.")


def test_arrival_past_midnight():
    options = [option("23:50", "24:40"), option("23:58", "24:35")]
    number, _, reason = pick(options)
    assert number == "2358" and "arrives 00:35" in reason


def test_settings_from_the_environment(monkeypatch):
    assert load_recommend_settings() == RecommendSettings()
    monkeypatch.setenv("SITT_WAIT_MAX_EXTRA_MINUTES", "8")
    monkeypatch.setenv("SITT_CROWD_GAIN_LEVELS", "2")
    monkeypatch.setenv("SITT_VERY_LATE_MINUTES", "12.5")
    monkeypatch.setenv("SITT_RECOMMEND_CANDIDATES", "7")
    monkeypatch.setenv("SITT_LADIES_SPECIAL_OK", "true")
    settings = load_recommend_settings()
    assert (settings.wait_max_extra_minutes, settings.crowd_gain_levels) == (8, 2)
    assert (settings.very_late_minutes, settings.candidates) == (12.5, 7)
    assert settings.ladies_special_ok is True
    monkeypatch.setenv("SITT_VERY_LATE_MINUTES", "soon")
    with pytest.raises(ValueError, match="SITT_VERY_LATE_MINUTES"):
        load_recommend_settings()


# --- Levels of prediction -----------------------------------------------------------------

MONDAY_7 = datetime(2026, 9, 28, 7, 0)


def _reading(con, when: datetime, number: str, station: str, delay, source="ntes", **extra):
    con.execute(
        "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source, "
        "train_number, event, cancelled, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            when.replace(tzinfo=IST),
            f"central-{number}",
            station,
            delay,
            source,
            number,
            extra.get("event", "at"),
            extra.get("cancelled", False),
            f"{when:%Y%m%dT%H%M}",
        ],
    )


def test_timetable_level_when_there_is_no_data(loaded):
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert (rec.level, rec.synthetic) == ("timetable", False)
    assert [o.trip.number for o in rec.options] == [
        "90104",
        "90106",
        "90108",
        "90110",
        "90102",  # the sample has only four more trains today; this is tomorrow's first
    ]
    assert all(o.arrival_delay == 0 and o.arrival_margin is None for o in rec.options)
    # 90106 leaves after 90104 but arrives first.
    assert rec.chosen.trip.number == "90106"
    assert rec.reason.startswith("Wait for the 07:12 fast: arrives 08:19, ")
    assert (rec.origin, rec.destination) == ("Kalyan", "Chhatrapati Shivaji Maharaj Terminus")
    assert "Timetable times only" in formatting.prediction_footer(rec)
    assert "SYNTHETIC" not in formatting.format_recommendation(rec) + explain(rec)


def test_an_aware_time_is_read_as_mumbai_time(loaded):
    utc = datetime(2026, 9, 28, 1, 30, tzinfo=UTC)  # 07:00 IST
    rec = recommend(loaded, "kalyan", "csmt", utc, model_dir=None)
    assert rec.asked_at == MONDAY_7 and rec.options[0].trip.number == "90104"


def test_baseline_level_uses_past_readings(loaded):
    # 90106 was 12-16 minutes late into CSMT on each of the last six days, and on time at Kalyan.
    for back, delay in enumerate([12, 13, 14, 15, 16, 14], start=1):
        day = MONDAY_7 - timedelta(days=back)
        _reading(loaded, day.replace(hour=8, minute=30), "90106", "CSMT", delay)
        _reading(loaded, day.replace(hour=7, minute=12), "90106", "KYN", 0)
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert (rec.level, rec.synthetic) == ("baseline", False)
    fast = next(o for o in rec.options if o.trip.number == "90106")
    assert (fast.departure_delay, fast.arrival_delay) == (0, 14)
    assert fast.predicted_arrival == datetime(2026, 9, 28, 8, 33)
    assert fast.arrival_margin == 2  # from the 10th-90th percentile of those six days
    # 90104 (arrives 08:24) now beats it.
    assert rec.chosen.trip.number == "90104"
    assert rec.notes == ["Some trains have no past readings; they are shown at timetable times."]
    assert "typical past delay of each train" in formatting.prediction_footer(rec)
    assert "(14 min late)" in formatting.format_recommendation(rec)


def test_synthetic_observations_flag_the_baseline(loaded):
    _reading(loaded, MONDAY_7 - timedelta(days=1), "90104", "KYN", 3, source="synthetic")
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    assert (rec.level, rec.synthetic) == ("baseline", True)
    assert "SYNTHETIC" in formatting.prediction_footer(rec)
    assert "SYNTHETIC: the delay figures come from invented data" in explain(rec)


def test_a_cancellation_reading_removes_a_train_and_crowds_the_next(loaded):
    _reading(
        loaded, MONDAY_7.replace(hour=6, minute=45), "90106", "", None,
        event="cancelled", cancelled=True,
    )  # fmt: skip
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    by_number = {o.trip.number: o for o in rec.options}
    assert by_number["90106"].cancelled and rec.chosen.trip.number != "90106"
    assert "the train before it is cancelled" in by_number["90108"].crowding.reason
    assert "· CANCELLED" in formatting.format_recommendation(rec)
    assert "reported CANCELLED" in explain(rec)
    # A cancellation from yesterday doesn't count.
    with init_db(":memory:") as fresh:
        load_timetable(fresh, read_timetable(SAMPLE_CSV))
        _reading(
            fresh, MONDAY_7 - timedelta(days=1), "90106", "", None,
            event="cancelled", cancelled=True,
        )  # fmt: skip
        assert not any(
            o.cancelled for o in recommend(fresh, "KYN", "CSMT", MONDAY_7, model_dir=None).options
        )


def test_starting_station_and_crowd_reports_reach_the_crowding_estimate(loaded):
    rec = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None)
    first = rec.options[0]  # 90104, a slow train starting at Kalyan at 07:03
    assert first.starts_here and "starts here" in first.crowding.reason
    before = first.crowding.score
    for _ in range(9):
        loaded.execute(
            "INSERT INTO crowd_reports (reported_at, train_description, station_code, "
            "crowd_level, source) VALUES (?, '07:05 slow from KYN', 'KYN', 5, 'telegram:1')",
            [(MONDAY_7 - timedelta(days=2)).replace(tzinfo=IST)],
        )
    after = recommend(loaded, "KYN", "CSMT", MONDAY_7, model_dir=None).options[0].crowding
    assert after.reports_used == 9 and after.score > before
    assert "your 9 reports average 5.0" in after.reason


@pytest.fixture(scope="module")
def synthetic_setup(tmp_path_factory):
    folder = tmp_path_factory.mktemp("recommend")
    timetable = folder / "timetable.duckdb"
    with init_db(timetable) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    db = folder / "synthetic.duckdb"
    build_database(timetable, db, folder / "out", date(2026, 6, 1), weeks=6, seed=5)
    with duckdb.connect(str(db), read_only=True) as con:
        model = train(con, TrainConfig(test_weeks=1, valid_weeks=1, rounds=30))
    model.save(folder / "model")
    return timetable, db, folder / "model"


def test_model_level_and_synthetic_flag_everywhere(synthetic_setup):
    timetable, _, model_dir = synthetic_setup
    with duckdb.connect(str(timetable), read_only=True) as con:  # the "real" DB: no observations
        rec = recommend(con, "KYN", "CSMT", MONDAY_7, model_dir=model_dir)
        without = recommend(con, "KYN", "CSMT", MONDAY_7, model_dir=model_dir.parent / "none")
    assert (rec.level, rec.synthetic) == ("model", True)
    assert without.level == "timetable"
    assert all(o.arrival_margin is not None for o in rec.options)
    reply = formatting.format_recommendation(rec)
    assert reply.splitlines()[-1] == (
        "Arrival times use the delay model, built from SYNTHETIC (invented) data, not real "
        "trains. Crowding is a rule-of-thumb estimate. /why for details."
    )
    why = explain(rec)
    assert "Predictions used: delay model, built from SYNTHETIC (invented) data" in why
    assert "say nothing about how real trains run" in why and "likely between" in why
