"""Timetable lookups and recommendations for `/next`. No Telegram imports here."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sitt.bot.parsing import parse_time
from sitt.bot.stations import Station, StationDirectory, directory_from
from sitt.config import RecommendSettings
from sitt.db import connect
from sitt.recommend import Recommendation, recommend
from sitt.timetable import ScheduledTrip, next_trains


class ScheduleError(ValueError):
    """A `/next` request that can't be answered. The message is shown to the user."""


@dataclass(frozen=True)
class Departures:
    origin: str  # station names, as stored in the timetable
    destination: str
    trips: list[ScheduledTrip]


def upcoming_trains(
    db_path: Path | str, origin: str, destination: str, now: datetime, n: int = 5
) -> Departures:
    """The next `n` scheduled trains from `origin` to `destination` after `now`.

    Stations are whatever the rider typed: an alias, a code or a name.
    """
    with connect(db_path) as con:
        if con.execute("SELECT count(*) FROM scheduled_stops").fetchone()[0] == 0:
            raise ScheduleError("The timetable isn't loaded yet, so I can't list trains.")
        stations = directory_from(con)
        start, end = _station(stations, origin), _station(stations, destination)
        if start == end:
            raise ScheduleError("Those are the same station.")
        trips = next_trains(con, start.code, end.code, now, n=n)
        return Departures(origin=start.name, destination=end.name, trips=trips)


def recommend_trains(
    db_path: Path | str,
    origin: str,
    destination: str,
    now: datetime,
    settings: RecommendSettings | None = None,
    model_dir: Path | str | None = None,
) -> Recommendation:
    """Compare the next trains and recommend one (see `sitt.recommend`).

    Stations are whatever the rider typed. `model_dir` is where a trained delay model
    may be; None means don't use one.
    """
    with connect(db_path) as con:
        if con.execute("SELECT count(*) FROM scheduled_stops").fetchone()[0] == 0:
            raise ScheduleError("The timetable isn't loaded yet, so I can't list trains.")
        stations = directory_from(con)
        start, end = _station(stations, origin), _station(stations, destination)
        if start == end:
            raise ScheduleError("Those are the same station.")
        return recommend(con, start.code, end.code, now, settings, model_dir=model_dir)


# A time asked for without a day that is more than this far in the past means tomorrow:
# at 19:00, "/next KYN CSMT 8:00" is about tomorrow morning, but "18:30" is about now-ish.
PAST_TIME_MEANS_TOMORROW = timedelta(hours=1)
_DAY_WORDS = {"today": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1}


@dataclass(frozen=True)
class When:
    """When a `/next` question is about."""

    moment: datetime
    explicit: bool  # the rider named a time or day; False means "now"


def split_when(words: Sequence[str], now: datetime) -> tuple[list[str], When]:
    """Take a trailing time and/or day off `/next` arguments.

    `["KYN", "CSMT", "8:00"]`, `[..., "tomorrow", "8:00"]`, `[..., "8", "pm"]` and
    `[..., "tomorrow"]` (this time of day, tomorrow) all work. A time with no day that is
    more than an hour gone today means tomorrow. Returns the remaining words and the moment
    asked about, in `now`'s time zone.
    """
    rest = list(words)
    asked_time = None
    day_offset = None
    for _ in range(3):  # at most a day word and a time, in either order
        if not rest:
            break
        last = rest[-1].lower()
        if last in _DAY_WORDS and day_offset is None:
            day_offset = _DAY_WORDS[last]
            rest.pop()
        elif asked_time is None and (found := parse_time(last)) is not None:
            asked_time = found
            rest.pop()
        elif (
            asked_time is None
            and last in ("am", "pm")
            and len(rest) >= 2
            and (found := parse_time(rest[-2] + last)) is not None
        ):
            asked_time = found
            del rest[-2:]
        else:
            break
    if asked_time is None and day_offset is None:
        return rest, When(now, explicit=False)
    day = now.date() + timedelta(days=day_offset or 0)
    if asked_time is None:
        moment = now + timedelta(days=day_offset or 0)
    else:
        moment = datetime.combine(day, asked_time, now.tzinfo)
        if day_offset is None and now - moment > PAST_TIME_MEANS_TOMORROW:
            moment += timedelta(days=1)
    return rest, When(moment, explicit=True)


def split_stations(words: Sequence[str], stations: StationDirectory) -> tuple[str, str] | None:
    """Split `/next` arguments into two stations, e.g. ["Kanjur", "Marg", "CSMT"].

    Returns None unless there are at least two words. Prefers a split where both halves
    are known stations; otherwise the first word is the origin and the rest the destination,
    so the error names the part that wasn't understood.
    """
    if len(words) < 2:
        return None
    halves = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))]
    for origin, destination in halves:
        if stations.lookup(origin) and stations.lookup(destination):
            return origin, destination
    return halves[0]


def _station(stations: StationDirectory, text: str) -> Station:
    station = stations.lookup(text)
    if station is None:
        raise ScheduleError(f"I don't know a station called {text!r}.")
    return station
