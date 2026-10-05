"""Mobond (m-Indicator) live-train feed: one GET returning `{train_no: status text}`.

Undocumented endpoint of a commercial app. See docs/data-sources.md §2.1: poll at most
every 15 minutes, and only once Mobond has given permission (collect.yml keeps it off
until the `SITT_MOBOND_ENABLED` repository variable is set).
"""

import json
import re
from datetime import UTC, datetime, timedelta

import httpx

from sitt.ingest.live.common import Observation, RawResponse, SourceError, request
from sitt.ingest.live.stations import resolve_station

SOURCE = "mobond"
URL = "https://mobond.com/mtracker/getalllivetrains"

# Every shape seen in three snapshots on 2026-09-27, e.g.
#   "[7 min ago] Between AMBARNATH - BADLAPUR, 36 min Late (Less Accurate)"
#   "Reaching CSMT (at VIDYAVIHAR now), 39 min Late"
#   "Cancellation Reported"
_STATUS_RE = re.compile(
    r"""
    ^(?:\[(?P<ago>\d+)\ min\ ago\]\ )?
    (?:
        (?P<cancelled>Cancellation\ Reported)
      | Rake\ at\ (?P<rake_at>[^,(]+?)
      | At\ (?P<at>[^,(]+?)
      | Crossed\ (?P<crossed>[^,(]+?)
      | Arriving\ (?P<arriving>[^,(]+?)
      | Between\ (?P<between>[^,(]+?)\ -\ (?P<between_next>[^,(]+?)
      | Reaching\ (?P<reaching>[^,(]+?)\ \(at\ (?P<reaching_now>[^)]+?)\ now\)
    )
    (?:,\ (?P<delay>\d+)\ min\ (?P<direction>Late|Early))?
    (?P<less_accurate>\ \(Less\ Accurate\))?$
    """,
    re.VERBOSE,
)


def fetch(client: httpx.Client) -> RawResponse:
    log: list[str] = []
    fetched_at = datetime.now(UTC)
    response = request(client, "GET", URL, log, headers={"Accept": "application/json"})
    return RawResponse(
        source=SOURCE,
        url=URL,
        fetched_at=fetched_at,
        status_code=response.status_code,
        content_type=response.headers.get("content-type", ""),
        body=response.text,
        requests=tuple(log),
    )


def parse_response(raw: RawResponse) -> list[Observation]:
    try:
        feed = json.loads(raw.body)
    except json.JSONDecodeError as exc:
        raise SourceError(f"Mobond response is not JSON: {exc}") from exc
    if not isinstance(feed, dict):
        raise SourceError(f"Mobond response is a JSON {type(feed).__name__}, expected an object")
    return [
        parse_status(str(number), str(status), raw.fetched_at) for number, status in feed.items()
    ]


def parse_status(train_number: str, status: str, fetched_at: datetime) -> Observation:
    """Turn one status string into an Observation. Unrecognised text is kept as event 'unknown'."""
    match = _STATUS_RE.match(status.strip())
    if match is None:
        return Observation(
            observed_at=fetched_at,
            train_number=train_number,
            station_code="",
            event="unknown",
            source=SOURCE,
            raw_status=status,
        )

    observed_at = fetched_at - timedelta(minutes=int(match["ago"] or 0))
    delay = None
    if match["delay"] is not None:
        delay = float(match["delay"]) * (-1 if match["direction"] == "Early" else 1)

    if match["cancelled"]:
        event, station_name = "cancelled", None
    elif match["reaching"]:
        # "Reaching CSMT (at SION now)": the train is at SION, heading for its terminus.
        event, station_name = "at", match["reaching_now"]
    else:
        event = next(
            key for key in ("rake_at", "at", "crossed", "arriving", "between") if match[key]
        )
        # For "between", station_code is the station the train last passed.
        station_name = match[event]

    # Only "at" pins the train to a station at a known instant, so only it gets a time.
    at_station = event == "at"
    return Observation(
        observed_at=observed_at,
        train_number=train_number,
        station_code=resolve_station(station_name, train_number) if station_name else "",
        event=event,
        source=SOURCE,
        delay_minutes=delay,
        actual_or_expected_time=observed_at if at_station else None,
        time_kind="actual" if at_station else None,
        cancelled=event == "cancelled",
        less_accurate=match["less_accurate"] is not None,
        raw_status=status,
    )
