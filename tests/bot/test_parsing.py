from datetime import time

import pytest

from sitt.bot.parsing import ParsedLog, parse_crowd_level, parse_log_text, parse_time


def test_spec_example():
    assert parse_log_text("8:12 fast KYN packed") == ParsedLog(
        station_code="KYN", departure_time=time(8, 12), service="fast", crowd_level=4
    )


def test_order_and_case_do_not_matter():
    assert parse_log_text("packed kyn FAST 8:12") == parse_log_text("8:12 fast KYN packed")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8:12", time(8, 12)),
        ("08:12", time(8, 12)),
        ("8.12", time(8, 12)),
        ("20:12", time(20, 12)),
        ("0:05", time(0, 5)),
        ("8:12pm", time(20, 12)),
        ("8:12 PM", time(20, 12)),
        ("8pm", time(20, 0)),
        ("8am", time(8, 0)),
        ("12am", time(0, 0)),
        ("12:30pm", time(12, 30)),
    ],
)
def test_parse_time_accepts(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["8", "812", "24:00", "8:60", "13pm", "0am", "8:1", "abc", ""])
def test_parse_time_rejects(text):
    assert parse_time(text) is None


def test_time_with_separate_ampm_token():
    assert parse_log_text("KYN 8:12 pm slow 2").departure_time == time(20, 12)


@pytest.mark.parametrize(
    ("token", "level"),
    [
        ("1", 1),
        ("5", 5),
        ("empty", 1),
        ("seats", 2),
        ("standing", 3),
        ("packed", 4),
        ("FULL", 4),
        ("cantboard", 5),
    ],
)
def test_parse_crowd_level(token, level):
    assert parse_crowd_level(token) == level


@pytest.mark.parametrize("token", ["0", "6", "10", "busy"])
def test_parse_crowd_level_rejects(token):
    assert parse_crowd_level(token) is None


@pytest.mark.parametrize(
    "phrase", ["can't board", "cant board", "cannot board", "can’t board", "Couldn't board"]
)
def test_cant_board_phrases(phrase):
    assert parse_log_text(f"8:12 fast KYN {phrase}").crowd_level == 5


@pytest.mark.parametrize(
    ("token", "code"),
    [("kyn", "KYN"), ("Kalyan", "KYN"), ("thane", "TNA"), ("VT", "CSMT"), ("dombivali", "DI")],
)
def test_station_by_code_name_or_alias(token, code):
    assert parse_log_text(f"{token} 8:12").station_code == code


def test_partial_input_leaves_fields_none():
    parsed = parse_log_text("TNA packed")
    assert (parsed.station_code, parsed.crowd_level) == ("TNA", 4)
    assert parsed.departure_time is None and parsed.service is None
    assert parsed.conflicts == ()


def test_empty_input():
    assert parse_log_text("") == ParsedLog()


def test_unrecognised_tokens_are_collected_not_fatal():
    parsed = parse_log_text("8:12, fast KYN packed! near the ladies coach")
    assert parsed.station_code == "KYN" and parsed.crowd_level == 4
    assert parsed.unrecognised == ("near", "the", "ladies", "coach")


def test_repeated_same_value_is_not_a_conflict():
    assert parse_log_text("KYN kalyan 4 packed").conflicts == ()


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("KYN TNA 8:12", "more than one station (KYN, TNA)"),
        ("KYN 8:12 9:00", "more than one time (08:12, 09:00)"),
        ("KYN fast slow", "both fast and slow"),
        ("KYN packed 2", "more than one crowd level (4, 2)"),
    ],
)
def test_conflicts(text, fragment):
    parsed = parse_log_text(text)
    assert fragment in parsed.conflicts
