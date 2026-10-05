"""Timetable lookups for `/next`. No Telegram imports here."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from sitt.bot.stations import lookup_station
from sitt.db import connect
from sitt.timetable import ScheduledTrip, UnknownStationError, next_trains, resolve_station


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
    """The next `n` scheduled trains from `origin` to `destination` after `now`."""
    with connect(db_path) as con:
        if con.execute("SELECT count(*) FROM scheduled_stops").fetchone()[0] == 0:
            raise ScheduleError("The timetable isn't loaded yet, so I can't list trains.")
        origin_code = _station_code(con, origin)
        destination_code = _station_code(con, destination)
        if origin_code == destination_code:
            raise ScheduleError("Those are the same station.")
        trips = next_trains(con, origin_code, destination_code, now, n=n)
        return Departures(
            origin=_station_name(con, origin_code),
            destination=_station_name(con, destination_code),
            trips=trips,
        )


def _station_code(con: duckdb.DuckDBPyConnection, token: str) -> str:
    """Resolve what the user typed: a code or name in the timetable, or a bot alias."""
    candidates = [token]
    if (station := lookup_station(token)) is not None:
        # Aliases such as "cst" or "sin", and stations whose code differs in the
        # loaded timetable, resolve through the bot's own code and name.
        candidates += [station.code, station.name]
    for candidate in candidates:
        try:
            return resolve_station(con, candidate)
        except UnknownStationError:
            continue
    raise ScheduleError(f"I don't know a station called {token!r}.")


def _station_name(con: duckdb.DuckDBPyConnection, code: str) -> str:
    return con.execute("SELECT name FROM stations WHERE code = ?", [code]).fetchone()[0]
