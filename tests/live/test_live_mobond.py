import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sitt.ingest.cr_pdf import _STATIONS
from sitt.ingest.live.common import RawResponse, SourceError
from sitt.ingest.live.mobond import parse_response, parse_status
from sitt.ingest.live.stations import _CENTRAL_CODES

FIXTURE = Path(__file__).parents[1] / "fixtures" / "live" / "mobond_getalllivetrains.json"
FETCHED = datetime(2026, 9, 27, 10, 24, tzinfo=UTC)


def _raw(body: str) -> RawResponse:
    return RawResponse(
        "mobond", "https://example.invalid", FETCHED, 200, "application/json", body, ()
    )


def test_fixture_parses_completely():
    body = FIXTURE.read_text(encoding="utf-8")
    observations = parse_response(_raw(body))
    assert len(observations) == len(json.loads(body)) == 40
    assert [o.raw_status for o in observations if o.event == "unknown"] == []


def test_fixture_is_invented_but_covers_every_status_shape():
    """The fixture is hand-written, not a copy of Mobond's feed, so it must be complete."""
    body = FIXTURE.read_text(encoding="utf-8")
    statuses = list(json.loads(body).values())
    observations = parse_response(_raw(body))

    # Every alternative of the parser's pattern, by the event it produces.
    events = {o.event for o in observations}
    assert events == {"at", "crossed", "arriving", "between", "rake_at", "cancelled"}
    assert any(s.startswith("Reaching ") for s in statuses)  # parsed as "at" its current station

    # Every optional part, present and absent, for each positional event.
    for event in ("at", "crossed", "arriving", "between"):
        group = [o for o in observations if o.event == event]
        assert {o.delay_minutes is None for o in group} == {True, False}, event
        assert {o.less_accurate for o in group} == {True, False}, event
        assert any((o.delay_minutes or 0) > 0 for o in group), event
        assert any((o.delay_minutes or 0) < 0 for o in group), event
    for prefix in ("At ", "Crossed ", "Between "):
        assert any(s.startswith("[") and f"] {prefix}" in s for s in statuses), prefix
    assert any(o.observed_at < FETCHED for o in observations)  # "[N min ago]"
    rakes = [o for o in observations if o.event == "rake_at"]
    assert {o.delay_minutes is None for o in rakes} == {True, False}
    assert sum(o.cancelled for o in observations) == 1

    # Central numbers resolve to codes; Western (90-94xxx) keep the raw station name.
    by_number = {o.train_number: o for o in observations}
    assert by_number["95012"].station_code == "DI"
    assert by_number["91010"].station_code == "DADAR"
    assert by_number["97016"].station_code == "ULNR"


@pytest.mark.parametrize(
    ("status", "event", "station", "delay", "less_accurate"),
    [
        ("At ULHAS NAGAR, 46 min Late", "at", "ULNR", 46, False),
        ("At KALYAN, 3 min Late (Less Accurate)", "at", "KYN", 3, True),
        ("At THANE", "at", "TNA", None, False),
        ("At SION, 2 min Early", "at", "SION", -2, False),
        ("Crossed KANJUR, 1 min Early", "crossed", "KJRD", -1, False),
        ("Arriving DIVA, 8 min Late", "arriving", "DIVA", 8, False),
        ("Between SHAHAD - AMBIVLI, 21 min Late", "between", "SHAD", 21, False),
        ("Reaching CSMT (at VIDYAVIHAR now), 39 min Late", "at", "VVH", 39, False),
        ("Rake at KALYAN", "rake_at", "KYN", None, False),
        ("At THANSIT, 18 min Late", "at", "THANSIT", 18, False),  # no known code: raw name
    ],
)
def test_parse_status_shapes(status, event, station, delay, less_accurate):
    observation = parse_status("95222", status, FETCHED)
    assert (observation.event, observation.station_code) == (event, station)
    assert observation.delay_minutes == delay
    assert observation.less_accurate is less_accurate
    assert observation.raw_status == status
    assert observation.source == "mobond"


def test_only_at_readings_carry_a_time():
    at = parse_status("95222", "At KALYAN, 3 min Late", FETCHED)
    assert (at.actual_or_expected_time, at.time_kind) == (FETCHED, "actual")
    between = parse_status("95222", "Between KALYAN - THAKURLI", FETCHED)
    assert (between.actual_or_expected_time, between.time_kind) == (None, None)


def test_staleness_prefix_moves_observed_at_back():
    observation = parse_status(
        "95229", "[7 min ago] Between AMBARNATH - BADLAPUR, 36 min Late (Less Accurate)", FETCHED
    )
    assert observation.observed_at == FETCHED - timedelta(minutes=7)
    assert (observation.event, observation.station_code) == ("between", "ABH")
    assert observation.less_accurate


def test_cancellation():
    observation = parse_status("95218", "Cancellation Reported", FETCHED)
    assert observation.cancelled
    assert (observation.event, observation.station_code) == ("cancelled", "")
    assert observation.delay_minutes is None


def test_western_railway_dadar_is_not_central_dadar():
    assert parse_status("95231", "Crossed DADAR", FETCHED).station_code == "DR"
    assert parse_status("92126", "At DADAR, 2 min Late", FETCHED).station_code == "DADAR"


def test_station_codes_agree_with_the_timetable():
    # Observations join to `stations` on these codes, so they must be the timetable's codes.
    assert set(_CENTRAL_CODES.values()) <= {station.code for station in _STATIONS}


def test_unrecognised_status_is_kept_not_dropped():
    observation = parse_status("95000", "Running late due to signal failure", FETCHED)
    assert (observation.event, observation.station_code) == ("unknown", "")
    assert observation.raw_status == "Running late due to signal failure"


@pytest.mark.parametrize("body", ["<html>maintenance</html>", "[1, 2]"])
def test_bad_body_raises(body):
    with pytest.raises(SourceError):
        parse_response(_raw(body))
