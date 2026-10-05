"""Timetable lookups for `/next`. No Telegram imports here."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sitt.bot.stations import Station, StationDirectory, directory_from
from sitt.db import connect
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
