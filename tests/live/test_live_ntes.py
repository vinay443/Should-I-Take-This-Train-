from datetime import UTC, datetime
from pathlib import Path

import pytest

from sitt.ingest.live.common import SourceError
from sitt.ingest.live.ntes import (
    parse_board,
    parse_csrf_token,
    parse_delay_badge,
    resolve_clock,
)
from sitt.tz import IST

FIXTURE = Path(__file__).parents[1] / "fixtures" / "live" / "ntes_live_station_kyn.html"
FETCHED = datetime(2026, 9, 27, 15, 51, tzinfo=IST)


@pytest.fixture(scope="module")
def board():
    observations = parse_board(FIXTURE.read_text(encoding="utf-8"), FETCHED, "KYN")
    return {(o.train_number, o.event): o for o in observations}


def _clock(hhmm: str, day: int = 27) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(2026, 9, day, hours, minutes, tzinfo=IST)


@pytest.mark.parametrize(
    ("train", "event", "time", "kind", "delay"),
    [
        ("12164", "arrival", "15:05", "actual", 48),  # unstarred time = actual
        ("12164", "departure", "15:06", "expected", 46),  # starred time = expected
        ("96205", "departure", "15:18", "expected", 0),
        ("95413", "departure", "15:47", "actual", 21),
        ("95222", "arrival", "15:57", "expected", 41),
        ("12072", "arrival", "15:44", "actual", 6),
        ("12162", "arrival", "18:59", "expected", 257),  # "04:17 Hrs." late
    ],
)
def test_board_rows(board, train, event, time, kind, delay):
    observation = board[(train, event)]
    assert observation.actual_or_expected_time == _clock(time)
    assert observation.time_kind == kind
    assert observation.delay_minutes == delay
    assert observation.station_code == "KYN"
    assert observation.observed_at == FETCHED
    assert not observation.cancelled


def test_source_and_destination_cells_are_skipped(board):
    assert ("96205", "arrival") not in board  # starts at Kalyan
    assert ("95222", "departure") not in board  # terminates at Kalyan
    assert len(board) == 9


def test_raw_status_describes_the_train(board):
    assert board[("12164", "arrival")].raw_status == "MAS LTT SF EXP (MAS-LTT) SUPERFAST"
    assert board[("95413", "departure")].raw_status == "N (KYN-KSRA) SUBURBAN"


def test_cancelled_cell_is_flagged():
    # Synthetic: cancellation wording hasn't appeared on a saved board, so this only checks
    # that a cell mentioning it is kept rather than silently skipped.
    page = (
        "<th>1 Trains departing from/arriving at <b>KYN</b></th>"
        "<tr><td>1</td><td><b>95413</b>&nbsp;|<b> N </b><br>"
        '<font size="2">(KYN-KSRA)&nbsp;SUBURBAN</font></td>'
        "<td>Source</td><td>Cancelled</td><td></td></tr></table>"
    )
    (observation,) = parse_board(page, FETCHED, "KYN")
    assert observation.cancelled
    assert (observation.event, observation.actual_or_expected_time) == ("departure", None)


def test_page_without_board_raises():
    with pytest.raises(SourceError):
        parse_board("<html>Service unavailable</html>", FETCHED, "KYN")


def test_resolve_clock_picks_the_nearest_day():
    near_midnight = datetime(2026, 9, 27, 23, 50, tzinfo=IST)
    assert resolve_clock("00:10", near_midnight) == _clock("00:10", day=28)
    assert resolve_clock("23:40", datetime(2026, 9, 28, 0, 5, tzinfo=IST)) == _clock("23:40")
    assert resolve_clock("15:05", datetime(2026, 9, 27, 10, 21, tzinfo=UTC)) == _clock("15:05")


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("On Time", 0), ("5 Mins.", 5), ("48 Mins.", 48), ("04:17 Hrs.", 257), ("RT", None)],
)
def test_parse_delay_badge(text, minutes):
    assert parse_delay_badge(text) == minutes


def test_parse_csrf_token():
    page = "<input type='hidden' name='aB3xY9' value='q7Z0kL2m'>"
    assert parse_csrf_token(page) == {"aB3xY9": "q7Z0kL2m"}
    assert parse_csrf_token("<html></html>") == {}
