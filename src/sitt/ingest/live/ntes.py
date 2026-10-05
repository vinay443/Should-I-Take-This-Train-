"""NTES (enquiry.indianrail.gov.in) Live Station board for one station, Kalyan by default.

The board is an HTML form behind a session cookie and a CSRF token, so one board costs
three sequential requests: the home page (cookie), the token, then the form POST.
See docs/data-sources.md §2.2.
"""

import html
import logging
import re
import time
from datetime import UTC, datetime, timedelta

import httpx

from sitt.ingest.live.common import Observation, RawResponse, SourceError, request
from sitt.tz import IST

logger = logging.getLogger(__name__)

SOURCE = "ntes"
BASE_URL = "https://enquiry.indianrail.gov.in/mntes/"
DEFAULT_STATION = "KYN"
DEFAULT_HOURS = 2  # the board offers 2, 4 or 8 hours ahead
PAUSE_SECONDS = 1.0  # gap between the three requests

_HEADER_RE = re.compile(r"(\d+) Trains departing from/arriving at")
_TOKEN_RE = re.compile(r"""name=['"]([^'"]+)['"]\s+value=['"]([^'"]*)['"]""")
_ROW_TRAIN_RE = re.compile(r"<b>(\d{4,5})</b>&nbsp;\|\s*<b>\s*(.*?)\s*</b>", re.S)
_ROW_ROUTE_RE = re.compile(r"\(([A-Z]+-[A-Z]+)\)&nbsp;([A-Z ]+)")
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
# Inside an arrival/departure cell: the live time (a trailing * means expected rather than
# actual), a delay badge, then the scheduled time in small print.
_LIVE_TIME_RE = re.compile(r'<font color="\w+">\s*(\d{1,2}:\d{2})(\*?)\s*</font>')
_BADGE_RE = re.compile(r'padding: ?1px 4px;">\s*([^<]*?)\s*</span>')
_SCHEDULED_RE = re.compile(r'<font size="1">(?:&nbsp;|\s)*(\d{1,2}:\d{2})\s*</font>')


def fetch(
    client: httpx.Client,
    station: str = DEFAULT_STATION,
    hours: int = DEFAULT_HOURS,
    pause: float | None = None,
) -> RawResponse:
    pause = PAUSE_SECONDS if pause is None else pause
    log: list[str] = []
    headers = {"Referer": BASE_URL}

    request(client, "GET", BASE_URL, log, headers=headers)  # sets the session cookie
    time.sleep(pause)
    token_page = request(
        client,
        "GET",
        f"{BASE_URL}GetCSRFToken",
        log,
        params={"t": int(time.time() * 1000)},
        headers=headers,
    )
    token = parse_csrf_token(token_page.text)
    if not token:
        raise SourceError("NTES returned no CSRF token", tuple(log))
    time.sleep(pause)

    fields = {
        "lan": "en",
        "jFromStationInput": station,
        "jToStationInput": "",
        "nHr": str(hours),
        "appLang": "en",
        "jStnName": "",
        "jStation": "",
        **token,
    }
    fetched_at = datetime.now(UTC)
    board = request(
        client,
        "POST",
        f"{BASE_URL}q",
        log,
        params={"opt": "LiveStation", "subOpt": "show"},
        data=fields,
        headers=headers,
    )
    return RawResponse(
        source=SOURCE,
        url=str(board.url),
        fetched_at=fetched_at,
        status_code=board.status_code,
        content_type=board.headers.get("content-type", ""),
        body=board.text,
        requests=tuple(log),
    )


def parse_csrf_token(page: str) -> dict[str, str]:
    """The hidden `<input name=... value=...>` pair(s) returned by GetCSRFToken."""
    return dict(_TOKEN_RE.findall(page))


def parse_response(raw: RawResponse, station: str = DEFAULT_STATION) -> list[Observation]:
    return parse_board(raw.body, raw.fetched_at, station)


def parse_board(page: str, fetched_at: datetime, station: str) -> list[Observation]:
    header = _HEADER_RE.search(page)
    if header is None:
        raise SourceError("NTES page has no Live Station table (error page or layout change?)")
    table = page[header.start() : page.find("</table>", header.start())]

    observations = []
    for row in table.split("<tr>")[1:]:
        train = _ROW_TRAIN_RE.search(row)
        cells = _CELL_RE.findall(row)
        if train is None or len(cells) < 4:
            continue
        number, name = train[1], " ".join(html.unescape(train[2]).split())
        route = _ROW_ROUTE_RE.search(row)
        description = f"{name} ({route[1]}) {route[2].strip()}" if route else name
        # Columns: Sr. | Train | Arrival | Departure | Platform
        for event, cell in (("arrival", cells[2]), ("departure", cells[3])):
            observation = _parse_cell(cell, event, number, description, station, fetched_at)
            if observation is not None:
                observations.append(observation)

    listed, parsed = int(header[1]), len({o.train_number for o in observations})
    if parsed < listed:
        # Every listed train normally has at least one timed cell; fewer hints at a layout change.
        logger.warning("NTES board lists %d trains but only %d were parsed", listed, parsed)
    return observations


def _parse_cell(
    cell: str, event: str, number: str, description: str, station: str, fetched_at: datetime
) -> Observation | None:
    live = _LIVE_TIME_RE.search(cell)
    if live is None:
        # "Source" / "Destination": the train starts or ends here, so there's nothing to record.
        # Cancellation wording hasn't been seen on a saved board yet; flag it if it appears.
        if "cancel" in cell.lower():
            return Observation(
                observed_at=fetched_at,
                train_number=number,
                station_code=station,
                event=event,
                source=SOURCE,
                cancelled=True,
                raw_status=description,
            )
        return None

    live_time = resolve_clock(live[1], fetched_at)
    scheduled = _SCHEDULED_RE.search(cell)
    if scheduled is not None:
        scheduled_time = resolve_clock(scheduled[1], live_time)
        delay = (live_time - scheduled_time).total_seconds() / 60
    else:
        badge = _BADGE_RE.search(cell)
        delay = parse_delay_badge(badge[1]) if badge else None

    return Observation(
        observed_at=fetched_at,
        train_number=number,
        station_code=station,
        event=event,
        source=SOURCE,
        delay_minutes=delay,
        actual_or_expected_time=live_time,
        time_kind="expected" if live[2] else "actual",
        raw_status=description,
    )


def resolve_clock(hhmm: str, near: datetime) -> datetime:
    """The IST datetime with this wall-clock time that is closest to `near`."""
    hours, minutes = map(int, hhmm.split(":"))
    local = near.astimezone(IST)
    candidates = (
        datetime(local.year, local.month, local.day, hours, minutes, tzinfo=IST)
        + timedelta(days=offset)
        for offset in (-1, 0, 1)
    )
    return min(candidates, key=lambda candidate: abs(candidate - local))


def parse_delay_badge(text: str) -> float | None:
    """ "On Time" -> 0, "21 Mins." -> 21, "04:17 Hrs." -> 257."""
    text = text.strip()
    if text.lower() == "on time":
        return 0.0
    if match := re.fullmatch(r"(\d+) Mins?\.?", text):
        return float(match[1])
    if match := re.fullmatch(r"(\d+):(\d{2}) Hrs?\.?", text):
        return float(int(match[1]) * 60 + int(match[2]))
    return None
