from datetime import UTC, datetime, timedelta

import pytest

from sitt.timetable import UnknownStationError, next_trains, resolve_station
from sitt.tz import IST

# 2026-09-28 is a Monday. Timings come from the invented sample timetable.
MONDAY = datetime(2026, 9, 28)
SUNDAY = datetime(2026, 9, 27)


def at(day, hhmm):
    hours, minutes = map(int, hhmm.split(":"))
    return day.replace(hour=hours, minute=minutes)


def summary(trips):
    return [
        (t.number, t.train_type, f"{t.departure:%a %H:%M}", f"{t.arrival:%a %H:%M}") for t in trips
    ]


def test_next_trains_kalyan_to_csmt(loaded):
    trips = next_trains(loaded, "KYN", "CSMT", at(MONDAY, "07:00"), n=4)
    assert summary(trips) == [
        ("90104", "slow", "Mon 07:03", "Mon 08:24"),
        ("90106", "fast", "Mon 07:12", "Mon 08:19"),
        ("90108", "fast", "Mon 07:31", "Mon 08:38"),
        ("90110", "slow", "Mon 07:48", "Mon 09:09"),
    ]
    assert trips[1].label == "CSMT"
    assert trips[1].direction == "up"
    assert trips[1].duration == timedelta(minutes=67)


def test_limit_and_rollover_to_next_day(loaded):
    trips = next_trains(loaded, "TNA", "CSMT", at(MONDAY, "23:00"), n=2)
    assert summary(trips) == [
        ("90102", "slow", "Tue 04:44", "Tue 05:36"),
        ("90104", "slow", "Tue 07:32", "Tue 08:24"),
    ]


def test_only_trains_going_the_right_way(loaded):
    trips = next_trains(loaded, "CSMT", "TNA", at(MONDAY, "07:00"), n=3)
    assert summary(trips) == [
        ("90105", "slow", "Mon 07:10", "Mon 08:02"),
        ("90107", "fast", "Mon 07:25", "Mon 08:10"),
        ("90113", "slow", "Mon 08:00", "Mon 08:52"),
    ]
    assert {t.direction for t in trips} == {"down"}


def test_fast_trains_skip_slow_only_stations(loaded):
    trips = next_trains(loaded, "Thane", "Kalwa", at(MONDAY, "06:00"), n=10)
    # Fast trains don't stop at Kalwa, and 90113 terminates at Thane.
    assert {t.train_type for t in trips} == {"slow"}
    assert [t.number for t in trips][:3] == ["90101", "90105", "90111"]


def test_running_days(loaded):
    monday = next_trains(loaded, "CSMT", "KYN", at(MONDAY, "07:00"), n=2)
    sunday = next_trains(loaded, "CSMT", "KYN", at(SUNDAY, "07:00"), n=2)
    assert [t.number for t in monday] == ["90105", "90107"]  # 90105 runs Mon-Sat
    assert [t.number for t in sunday] == ["90107", "90109"]  # 90109 runs Sundays only


def test_train_running_past_midnight(loaded):
    evening = next_trains(loaded, "CSMT", "KYN", at(MONDAY, "23:30"), n=1)
    assert summary(evening) == [("90111", "slow", "Mon 23:52", "Tue 01:13")]
    assert evening[0].duration == timedelta(minutes=81)

    # After midnight, Monday's 23:52 from CSMT is still to come at Thane.
    night = next_trains(loaded, "TNA", "KYN", at(MONDAY + timedelta(days=1), "00:30"), n=2)
    assert summary(night) == [
        ("90111", "slow", "Tue 00:45", "Tue 01:13"),
        ("90101", "slow", "Tue 06:55", "Tue 07:23"),
    ]


def test_train_leaving_this_minute_is_included(loaded):
    trips = next_trains(loaded, "KYN", "CSMT", at(MONDAY, "07:03").replace(second=40), n=1)
    assert trips[0].number == "90104"


def test_aware_datetime_is_converted_to_ist(loaded):
    when = datetime(2026, 9, 28, 1, 30, tzinfo=UTC)  # 07:00 IST
    trips = next_trains(loaded, "KYN", "CSMT", when, n=1)
    assert trips[0].departure == datetime(2026, 9, 28, 7, 3, tzinfo=IST)


def test_resolve_station_by_code_or_name(loaded):
    assert resolve_station(loaded, "kyn") == "KYN"
    assert resolve_station(loaded, " kalyan ") == "KYN"
    with pytest.raises(UnknownStationError):
        resolve_station(loaded, "Nowhere")


def test_bad_arguments(loaded):
    with pytest.raises(ValueError, match="same station"):
        next_trains(loaded, "KYN", "Kalyan", MONDAY)
    with pytest.raises(ValueError, match="at least 1"):
        next_trains(loaded, "KYN", "CSMT", MONDAY, n=0)


def test_empty_database_returns_no_trains(con):
    con.execute("INSERT INTO stations VALUES ('A', 'A', 'central', 1), ('B', 'B', 'central', 2)")
    assert next_trains(con, "A", "B", MONDAY) == []


# --- Why no train goes there ---------------------------------------------------------


@pytest.fixture
def both_lines(loaded, tmp_path):
    """The sample main line plus real Harbour DOWN trains (CSMT, Masjid, Sandhurst Road and
    Kurla are on both)."""
    from conftest import harbour_csv

    from sitt.ingest.timetable import load_timetable, read_timetable

    load_timetable(loaded, read_timetable(harbour_csv(tmp_path), line="harbour"))
    return loaded


def test_stations_on_different_lines_say_where_the_lines_meet(both_lines):
    from sitt.timetable import no_train_reason

    # (The sample timetable spells CSMT out in full.)
    assert no_train_reason(both_lines, "KYN", "PNVL") == (
        "No train runs from Kalyan to Panvel without a change: Kalyan is on the Central line, "
        "Panvel on the Harbour line. They meet at Chhatrapati Shivaji Maharaj Terminus, "
        "Masjid, Sandhurst Road and Kurla: ask for each part separately."
    )


def test_a_direction_whose_timetable_is_not_loaded_is_named(both_lines):
    from sitt.timetable import no_train_reason

    reason = no_train_reason(both_lines, "PNVL", "CSMT")
    assert reason.startswith(
        "The Harbour line timetable loaded here has no trains towards CSMT, so none from "
        "Panvel to Chhatrapati Shivaji Maharaj Terminus: only its DOWN timetable is loaded"
    )


def test_two_branches_of_one_line(both_lines):
    from sitt.timetable import no_train_reason

    # Bandra is on the Goregaon branch, Vashi on the Panvel branch: no train calls at both.
    assert no_train_reason(both_lines, "BA", "VSH") == (
        "No single train in the timetable runs from Bandra to Vashi."
    )


def test_a_served_route_has_no_reason(both_lines):
    from sitt.timetable import no_train_reason

    assert no_train_reason(both_lines, "CSMT", "PNVL") is None
    assert no_train_reason(both_lines, "KYN", "CSMT") is None
    assert no_train_reason(both_lines, "CSMT", "KYN") is None


def test_the_recommendation_gives_the_reason(both_lines):
    from sitt.recommend import recommend

    rec = recommend(both_lines, "KYN", "PNVL", at(MONDAY, "08:00"), model_dir=None)
    assert rec.choice is None and rec.options == []
    assert rec.reason.startswith("No train runs from Kalyan to Panvel without a change")
