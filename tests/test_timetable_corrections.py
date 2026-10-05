"""Stray marks in PDF cells, the manual overrides file, and the movable holidays.

Every grid here is synthetic: a few invented columns over real station names. No PDF is
read and nothing is fetched.
"""

from datetime import date, datetime

import pytest

from sitt import holidays
from sitt.ingest import overrides
from sitt.ingest.cr_pdf import (
    Column,
    ConvertedTrain,
    PageGrid,
    StopTime,
    apply_supplement,
    grid_trains,
    station_for,
)
from sitt.ingest.overrides import OverrideError, apply_overrides, parse_overrides
from sitt.timetable import next_trains

ROWS = ("CSMT", "Byculla", "Dadar", "Kurla", "Vidyavihar", "Ghatkopar", "Thane")
# Shaped like train 95901 (T 15) in the real PDF: fast, passing most stations.
FAST = {"CSMT": "08:04", "Byculla": "08:11", "Dadar": "08:17", "Kurla": "08:24",
        "Vidyavihar": "…", "Ghatkopar": "08:28", "Thane": "08:46"}  # fmt: skip
SLOW = {"CSMT": "10:00", "Byculla": "10:07", "Dadar": "10:12", "Kurla": "10:20",
        "Vidyavihar": "10:23", "Ghatkopar": "10:26", "Thane": "10:44"}  # fmt: skip


def grid(cells: dict[str, str], markers=("T 15", "AC", "X"), number="95901") -> PageGrid:
    stations = [station_for(name) for name in ROWS]
    column = Column(number, page=3, markers=list(markers))
    column.cells = {station_for(name): value for name, value in cells.items()}
    return PageGrid(page=3, stations=stations, columns=[column])


def stops(train: ConvertedTrain) -> list[tuple[str, str]]:
    return [(stop.station.code, stop.time) for stop in train.stops]


# --- stray marks (train 95901) ---


def test_the_95901_backtick_is_read_as_a_pass_mark_and_reported():
    # The real PDF has "`" at Vidyavihar where the train passes, with "…" at Byculla...
    cells = {**FAST, "Byculla": "…", "Vidyavihar": "`"}
    conversion = grid_trains([grid(cells)])
    assert conversion.rejected == {}
    (train,) = conversion.trains
    assert (train.number, train.service_type, train.ac, train.days) == (
        "95901", "fast", True, "mon-sat",
    )  # fmt: skip
    assert stops(train) == [
        ("CSMT", "08:04"), ("DR", "08:17"), ("CLA", "08:24"), ("GC", "08:28"), ("TNA", "08:46"),
    ]  # fmt: skip
    assert conversion.warnings == [
        "train 95901: page 3: stray '`' at Vidyavihar read as a pass mark"
    ]


def test_a_stray_mark_in_a_train_with_no_pass_marks_still_rejects_it():
    # In an all-stops train the mark may be all that is left of a stop's time.
    conversion = grid_trains([grid({**SLOW, "Vidyavihar": "`"}, markers=("K 1",))])
    assert conversion.trains == []
    assert conversion.rejected == {"95901": "page 3: unreadable cell '`' at Vidyavihar"}


def test_a_lone_stray_mark_does_not_make_a_slow_train_fast():
    # The only "pass" would be the stray mark itself: not enough to call the train fast.
    all_but = {**SLOW, "Vidyavihar": "'"}
    assert "95901" in grid_trains([grid(all_but, markers=("K 1",))]).rejected


def test_stray_marks_around_a_time_are_ignored_and_reported():
    conversion = grid_trains([grid({**SLOW, "Dadar": "10:12`"}, markers=("K 1",))])
    (train,) = conversion.trains
    assert ("DR", "10:12") in stops(train) and train.service_type == "slow"
    assert conversion.warnings == ["train 95901: page 3: stray marks in '10:12`' at Dadar ignored"]


def test_a_stray_mark_outside_the_run_is_ignored():
    ends_at_ghatkopar = {**SLOW, "Thane": "`"}
    conversion = grid_trains([grid(ends_at_ghatkopar, markers=("K 1",))])
    (train,) = conversion.trains
    assert train.destination == "Ghatkopar"
    assert conversion.warnings == [
        "train 95901: page 3: stray '`' at Thane, outside the run, ignored"
    ]


def test_other_unreadable_cells_are_still_rejected():
    for bad in ("TXL", "8:6", "25:00", "`x`"):
        conversion = grid_trains([grid({**FAST, "Vidyavihar": bad})])
        assert conversion.trains == [], bad
        assert f"unreadable cell {bad!r} at Vidyavihar" in conversion.rejected["95901"]


def test_the_supplement_reports_the_stray_mark_too():
    trains: dict[str, ConvertedTrain] = {}
    cells = {**FAST, "Byculla": "…", "Vidyavihar": "`"}
    result = apply_supplement(trains, [grid(cells)], "ac")
    assert "95901" in trains and result.added == 1
    assert "95901: note: page 3: stray '`' at Vidyavihar read as a pass mark" in result.lines


# --- the overrides file ---


def base() -> dict[str, ConvertedTrain]:
    conversion = grid_trains([grid(SLOW, markers=("K 1",), number="96001"), grid(FAST)])
    return {train.number: train for train in conversion.trains}


def test_the_shipped_overrides_file_is_valid():
    assert overrides.default_path().name == "timetable_overrides.toml"
    assert overrides.read_overrides() == []  # no corrections at present


def test_set_changes_fields_and_single_stop_times():
    trains = base()
    entries = parse_overrides(
        """
        [[train]]
        number = "96001"
        reason = "PTT prints 10:12 at Dadar; the UP page gives 10:13."
        days = "mon-sat"
        cars = 15
        notes = ["ladies_special"]
        stop_times = { DR = "10:13" }
        remove_stops = ["VVH"]
        """
    )
    lines = apply_overrides(trains, entries)
    train = trains["96001"]
    assert (train.days, train.cars, train.notes) == ("mon-sat", 15, ["ladies_special"])
    assert ("DR", "10:13") in stops(train) and "VVH" not in dict(stops(train))
    assert lines == ["96001: changed. Reason: PTT prints 10:12 at Dadar; the UP page gives 10:13."]
    assert stops(trains["95901"])[0] == ("CSMT", "08:04")  # other trains untouched


def test_a_stop_added_by_time_lands_in_route_order_for_either_direction():
    trains = base()
    apply_overrides(
        trains,
        parse_overrides(
            '[[train]]\nnumber = "95901"\nreason = "r"\nstop_times = { SION = "08:21" }'
        ),
    )
    codes = [code for code, _ in stops(trains["95901"])]
    assert codes == ["CSMT", "BY", "DR", "SION", "CLA", "GC", "TNA"]

    up = ConvertedTrain(
        "96002", "up", "fast", "daily",
        [StopTime(station_for("Thane"), "09:00"), StopTime(station_for("CSMT"), "09:40")], 1,
    )  # fmt: skip
    trains = {"96002": up}
    apply_overrides(
        trains,
        parse_overrides('[[train]]\nnumber = "96002"\nreason = "r"\nstop_times = { DR = "09:28" }'),
    )
    assert stops(trains["96002"]) == [("TNA", "09:00"), ("DR", "09:28"), ("CSMT", "09:40")]


def test_add_and_remove():
    trains = base()
    entries = parse_overrides(
        """
        [[train]]
        number = "95999"
        action = "add"
        reason = "In the CR notice of 2026-11-01, not yet in a PTT PDF."
        direction = "down"
        service_type = "fast"
        days = "mon-fri"
        service_code = "K 99"
        ac = true
        stops = [["CSMT", "10:00"], ["DR", "10:12"], ["TNA", "10:40"], ["KYN", "11:02"]]

        [[train]]
        number = "96001"
        action = "remove"
        reason = "Withdrawn from 2026-11-01."
        """
    )
    lines = apply_overrides(trains, entries)
    assert sorted(trains) == ["95901", "95999"]
    added = trains["95999"]
    assert (added.direction, added.service_type, added.days, added.ac, added.service_code) == (
        "down", "fast", "mon-fri", True, "K 99",
    )  # fmt: skip
    assert added.destination == "Kalyan"
    assert lines[0].startswith("95999: added, fast down (CSMT 10:00 - Kalyan 11:02, 4 stops).")
    assert lines[1] == "96001: removed. Reason: Withdrawn from 2026-11-01."


def test_an_entry_that_changes_nothing_says_so():
    trains = base()
    entries = parse_overrides('[[train]]\nnumber = "96001"\nreason = "r"\ndays = "daily"')
    assert apply_overrides(trains, entries) == [
        "96001: no change; the timetable already says this. The entry can probably be deleted"
    ]


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[[train]]\nnumber = 96001\nreason = "r"\ncars = 15', "5-digit train number in quotes"),
        ('[[train]]\nnumber = "96001"\ncars = 15', "reason is required"),
        ('[[train]]\nnumber = "96001"\nreason = "r"', "nothing to change"),
        ('[[train]]\nnumber = "96001"\nreason = "r"\ncolour = "red"', "unknown key 'colour'"),
        ('[[train]]\nnumber = "96001"\nreason = "r"\naction = "fix"\ncars = 15', "action must be"),
        ('[[train]]\nnumber = "96001"\nreason = "r"\ncars = 9', "cars must be 12 or 15"),
        ('[[train]]\nnumber = "96001"\nreason = "r"\ndays = "weekdays"', "invalid days"),
        ('[[train]]\nnumber = "96001"\nreason = "r"\nac = "yes"', "ac must be true or false"),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\nstop_times = { XYZ = "10:00" }',
            "unknown station code 'XYZ'",
        ),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\nstop_times = { DR = "25:00" }',
            "'25:00' at DR is not a time",
        ),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\naction = "add"\nstops = [["DR", "10:00"]]',
            'action "add" needs direction',
        ),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\naction = "remove"\ncars = 15',
            'action "remove" takes nothing else',
        ),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\ndirection = "up"\ncars = 15',
            "can't be changed",
        ),
        ('[[trains]]\nnumber = "96001"', "unknown top-level key 'trains'"),
        ("[[train]\nnumber =", "not valid TOML"),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\ncars = 15\n'
            '[[train]]\nnumber = "96001"\nreason = "r"\nac = true',
            "already has entry 1",
        ),
    ],
)
def test_a_bad_overrides_file_is_refused_with_a_clear_message(toml, message):
    with pytest.raises(OverrideError, match=message):
        parse_overrides(toml)


def test_every_problem_in_a_file_is_reported_at_once():
    with pytest.raises(OverrideError) as error:
        parse_overrides(
            '[[train]]\nnumber = "96001"\ncars = 9\n[[train]]\nnumber = "x"\nreason = "r"'
        )
    text = str(error.value)
    assert "has 3 problem(s)" in text
    assert "entry 1 (train 96001): reason is required" in text and "entry 2: number" in text


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[[train]]\nnumber = "97777"\nreason = "r"\ncars = 15', "no such train"),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\naction = "add"\ndirection = "down"\n'
            'service_type = "slow"\nstops = [["CSMT", "10:00"], ["DR", "10:12"]]',
            "already exists",
        ),
        ('[[train]]\nnumber = "96001"\nreason = "r"\nremove_stops = ["KYN"]', "no stop at KYN"),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\nstop_times = { DR = "14:00" }',
            "doesn't follow",
        ),
        (
            '[[train]]\nnumber = "96001"\nreason = "r"\nstops = [["CSMT", "10:00"]]',
            "fewer than two stops",
        ),
    ],
)
def test_overrides_that_cannot_apply_change_nothing(toml, message):
    trains = base()
    before = {number: stops(train) for number, train in trains.items()}
    good = '[[train]]\nnumber = "95901"\nreason = "r"\ncars = 15\n'
    with pytest.raises(OverrideError, match=message):
        apply_overrides(trains, parse_overrides(good + toml))
    assert {number: stops(train) for number, train in trains.items()} == before
    assert trains["95901"].cars is None  # the valid entry wasn't applied either


# --- movable holidays ---


def test_the_shipped_holiday_file_is_valid_and_sourced():
    assert len(holidays.MOVABLE) >= 16
    for holiday in holidays.MOVABLE:
        assert holiday.status in holidays.STATUSES and holiday.source
        assert (holiday.date.month, holiday.date.day) not in holidays.FIXED_HOLIDAYS
    # The railway's list names eight movable days a year.
    for year in (2026, 2027):
        names = sorted(h.name for h in holidays.MOVABLE if h.date.year == year)
        assert names == sorted(
            ["Holi (2nd day)", "Gudi Padwa", "Good Friday", "Ramzan-Id", "Ganesh Chaturthi",
             "Dassera", "Diwali (1st day)", "Diwali (2nd day)"]
        )  # fmt: skip
    assert holidays.years_covered() == {2026: "complete", 2027: "tentative"}


def test_good_friday_is_two_days_before_easter():
    # Easter Sunday: 5 April 2026 and 28 March 2027 (Gregorian computus).
    good_fridays = {h.date for h in holidays.MOVABLE if h.name == "Good Friday"}
    assert good_fridays == {date(2026, 4, 3), date(2027, 3, 26)}
    assert all(h.status == "confirmed" for h in holidays.MOVABLE if h.name == "Good Friday")


def test_tentative_dates_are_not_used_unless_asked_for(monkeypatch):
    monkeypatch.delenv(holidays.INCLUDE_TENTATIVE_VARIABLE, raising=False)
    dassera_2026, ganesh_2027 = date(2026, 10, 20), date(2027, 9, 4)
    assert holidays.is_holiday(dassera_2026) and holidays.is_sunday_schedule(dassera_2026)
    assert dassera_2026.weekday() == 1  # a Tuesday
    assert not holidays.is_holiday(ganesh_2027)  # 2027 isn't notified yet
    assert not holidays.is_holiday(date(2026, 11, 9))  # the Monday between the Diwali days

    monkeypatch.setenv(holidays.INCLUDE_TENTATIVE_VARIABLE, "true")
    assert holidays.is_holiday(ganesh_2027) and holidays.is_holiday(dassera_2026)
    assert ganesh_2027 in holidays.movable_dates() - holidays.movable_dates(tentative=False)
    # Fixed dates work for any year, either way.
    assert holidays.is_holiday(date(2031, 1, 26))


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[[holiday]]\ndate = "2026-10-20"\nname = "x"\nstatus = "confirmed"\nsource = "s"',
         "without quotes"),
        ('[[holiday]]\ndate = 2026-10-20\nname = "x"\nstatus = "sure"\nsource = "s"',
         "status must be one of"),
        ('[[holiday]]\ndate = 2026-10-20\nname = "x"\nstatus = "confirmed"', "source is required"),
        ('[[holiday]]\ndate = 2026-01-26\nname = "x"\nstatus = "confirmed"\nsource = "s"',
         "already a fixed holiday"),
        ('[[holiday]]\ndate = 2026-10-20\nname = "x"\nstatus = "confirmed"\nsource = "s"\n'
         '[[holiday]]\ndate = 2026-10-20\nname = "y"\nstatus = "confirmed"\nsource = "s"',
         "the same date as entry 1"),
        ('[[holiday]]\ndate = 2026-10-20\nname = "x"\nstatus = "confirmed"\nsource = "s"\n'
         'colour = 1', "unknown key 'colour'"),
    ],
)  # fmt: skip
def test_a_bad_holiday_file_is_refused(toml, message):
    with pytest.raises(holidays.HolidayFileError, match=message):
        holidays.parse_holidays(toml)


def test_next_trains_uses_the_sunday_timetable_on_a_holiday(loaded):
    # Make one sample train weekdays-only and another Sundays-only.
    loaded.execute(
        "UPDATE scheduled_stops SET days_of_operation = 'YYYYYYN' WHERE train_id = 'central-90104'"
    )
    loaded.execute(
        "UPDATE scheduled_stops SET days_of_operation = 'NNNNNNY' WHERE train_id = 'central-90106'"
    )

    def numbers(day: date) -> list[str]:
        when = datetime(day.year, day.month, day.day, 7, 0)
        return [t.number for t in next_trains(loaded, "KYN", "CSMT", when, n=2)]

    assert numbers(date(2026, 10, 19))[0] == "90104"  # an ordinary Monday
    assert numbers(date(2026, 10, 18))[0] == "90106"  # a Sunday
    assert numbers(date(2026, 10, 20))[0] == "90106"  # Dassera, a Tuesday: Sunday timetable
    assert numbers(date(2026, 10, 2))[0] == "90106"  # Gandhi Jayanti, a Friday
