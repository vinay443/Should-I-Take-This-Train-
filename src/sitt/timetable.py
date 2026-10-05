"""Queries over the static timetable (loaded by sitt.ingest.timetable)."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import duckdb

from sitt.holidays import is_sunday_schedule
from sitt.tz import IST


class UnknownStationError(LookupError):
    pass


@dataclass(frozen=True)
class ScheduledTrip:
    """One train's scheduled journey between two stations."""

    train_id: str
    number: str | None
    label: str  # destination board text, e.g. "Kalyan"
    train_type: str  # "fast" or "slow"
    direction: str  # "up" (towards CSMT) or "down"
    departure: datetime  # from the origin
    arrival: datetime  # at the destination
    # Optional attributes; None when the timetable source didn't give them.
    service_code: str | None = None  # e.g. "A 1"
    is_ac: bool | None = None
    car_count: int | None = None
    is_ladies_special: bool | None = None
    ac_weekdays_only: bool | None = None  # AC rake that runs without AC at weekends
    service_day: date | None = None  # the day this run starts from its first station

    @property
    def runs_ac(self) -> bool | None:
        """Whether this particular run is air-conditioned. None if the timetable doesn't say."""
        if not self.is_ac:
            return self.is_ac
        weekend = self.service_day is not None and self.service_day.weekday() >= 5
        return not (self.ac_weekdays_only and weekend)

    @property
    def duration(self) -> timedelta:
        return self.arrival - self.departure


def resolve_station(con: duckdb.DuckDBPyConnection, station: str) -> str:
    """Return the station code for a code or name, ignoring case ('kyn', 'Kalyan')."""
    row = con.execute(
        """
        SELECT code FROM stations
        WHERE lower(code) = lower($s) OR lower(name) = lower($s)
        ORDER BY lower(code) = lower($s) DESC
        LIMIT 1
        """,
        {"s": station.strip()},
    ).fetchone()
    if row is None:
        raise UnknownStationError(f"unknown station {station!r}")
    return row[0]


def next_trains(
    con: duckdb.DuckDBPyConnection,
    origin: str,
    destination: str,
    when: datetime,
    n: int = 5,
) -> list[ScheduledTrip]:
    """The next `n` trains from `origin` to `destination` leaving at or after `when`.

    Stations may be given by code or name. A naive `when` is taken as Mumbai
    local time and the results are naive too; an aware `when` gives results in
    IST. Trains leaving earlier in the same minute as `when` are included.
    Running days apply to the day a train starts its run, so a train that
    leaves CSMT at 23:52 on a Sunday-only service reaches Thane at 00:45 on
    Monday. A holiday in `sitt.holidays` runs the Sunday timetable, whatever
    day of the week it falls on. The search covers the rest of today and all
    of tomorrow.
    """
    if n < 1:
        raise ValueError("n must be at least 1")
    origin_code = resolve_station(con, origin)
    destination_code = resolve_station(con, destination)
    if origin_code == destination_code:
        raise ValueError("origin and destination are the same station")

    tz = None
    if when.tzinfo is not None:
        tz = IST
        when = when.astimezone(IST).replace(tzinfo=None)
    when = when.replace(second=0, microsecond=0)

    rows = con.execute(
        """
        WITH stops AS (
            SELECT *,
                   first_value(coalesce(scheduled_departure, scheduled_arrival))
                       OVER (PARTITION BY train_id ORDER BY stop_seq) AS run_start
            FROM scheduled_stops
        )
        SELECT t.train_id, t.number, t.label, t.train_type, t.direction, o.run_start,
               coalesce(o.scheduled_departure, o.scheduled_arrival),
               coalesce(d.scheduled_arrival, d.scheduled_departure),
               o.days_of_operation, d.days_of_operation,
               t.service_code, t.is_ac, t.car_count, t.is_ladies_special,
               t.ac_weekdays_only
        FROM trains t
        JOIN stops o ON o.train_id = t.train_id AND o.station_code = $origin
        JOIN stops d ON d.train_id = t.train_id AND d.station_code = $destination
        WHERE o.stop_seq < d.stop_seq
        """,
        {"origin": origin_code, "destination": destination_code},
    ).fetchall()

    trips = []
    for day_offset in (-1, 0, 1):
        service_day = when.date() + timedelta(days=day_offset)
        weekday = 6 if is_sunday_schedule(service_day) else service_day.weekday()
        for row in rows:
            train_id, number, label, train_type, direction, start, dep, arr, o_days, d_days = row[
                :10
            ]
            service_code, is_ac, car_count, is_ladies_special, ac_weekdays_only = row[10:]
            if o_days[weekday] != "Y" or d_days[weekday] != "Y":
                continue
            departure = _on_service_day(service_day, start, dep)
            if departure < when:
                continue
            trips.append(
                ScheduledTrip(
                    train_id=train_id,
                    number=number,
                    label=label,
                    train_type=train_type,
                    direction=direction,
                    departure=departure.replace(tzinfo=tz),
                    arrival=_on_service_day(service_day, start, arr).replace(tzinfo=tz),
                    service_code=service_code,
                    is_ac=is_ac,
                    car_count=car_count,
                    is_ladies_special=is_ladies_special,
                    ac_weekdays_only=ac_weekdays_only,
                    service_day=service_day,
                )
            )
    trips.sort(key=lambda trip: (trip.departure, trip.arrival, trip.train_id))
    return trips[:n]


def _on_service_day(service_day: date, run_start: time, t: time) -> datetime:
    """Place a stop time on the calendar. Times earlier than the run's start are past midnight."""
    day = service_day + timedelta(days=1) if t < run_start else service_day
    return datetime.combine(day, t)
