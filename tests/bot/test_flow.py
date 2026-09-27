from datetime import datetime, time

import pytest

from sitt.bot.flow import (
    CANCEL_DATA,
    IST,
    LogDraft,
    callback_data,
    parse_callback_data,
    time_choices,
)
from sitt.bot.parsing import parse_log_text


def test_draft_from_complete_parse_is_complete():
    draft = LogDraft.from_parsed(
        parse_log_text("8:12 fast KYN packed"), raw_text="/log 8:12 fast KYN packed"
    )
    assert draft.is_complete
    assert draft.raw_text == "/log 8:12 fast KYN packed"
    assert draft.train_description() == "08:12 fast from KYN"


def test_steps_walk_in_order_and_skip_prefilled_fields():
    draft = LogDraft.from_parsed(parse_log_text("fast"), raw_text="/log fast")
    assert draft.next_step() == "station"
    draft.apply("station", "KYN")
    assert draft.next_step() == "time"
    draft.apply("time", "08:10")
    assert draft.next_step() == "crowd"  # service already known
    draft.apply("crowd", "5")
    assert draft.is_complete
    assert (draft.station_code, draft.departure_time, draft.service, draft.crowd_level) == (
        "KYN",
        time(8, 10),
        "fast",
        5,
    )


@pytest.mark.parametrize(
    ("step", "value"),
    [("station", "XYZ"), ("time", "25:00"), ("service", "medium"), ("crowd", "9"), ("bogus", "1")],
)
def test_apply_rejects_bad_values(step, value):
    with pytest.raises(ValueError):
        LogDraft(raw_text="/log").apply(step, value)


def test_callback_data_round_trip_and_fits_telegram_limit():
    data = callback_data("time", "08:10")
    assert parse_callback_data(data) == ("time", "08:10")
    assert len(data.encode()) <= 64


@pytest.mark.parametrize(
    "data", [CANCEL_DATA, "log:station:", "other:station:KYN", "log:nope:1", ""]
)
def test_parse_callback_data_rejects(data):
    with pytest.raises(ValueError):
        parse_callback_data(data)


def test_time_choices_floor_to_five_minutes():
    choices = time_choices(datetime(2026, 9, 27, 8, 13, 42, tzinfo=IST), offsets=(-10, 0, 10))
    assert choices == [time(8, 0), time(8, 10), time(8, 20)]


def test_time_choices_wrap_midnight():
    choices = time_choices(datetime(2026, 9, 27, 0, 7, tzinfo=IST), offsets=(-10, 0))
    assert choices == [time(23, 55), time(0, 5)]
