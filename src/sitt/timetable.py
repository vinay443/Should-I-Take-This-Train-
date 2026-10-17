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
    line: str | None = None  # trains.line, e.g. 'central' or 'harbour'

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
               t.ac_weekdays_only, t.line
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
            service_code, is_ac, car_count, is_ladies_special, ac_weekdays_only, line = row[10:]
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
                    line=line,
                )
            )
    trips.sort(key=lambda trip: (trip.departure, trip.arrival, trip.train_id))
    return trips[:n]


def _on_service_day(service_day: date, run_start: time, t: time) -> datetime:
    """Place a stop time on the calendar. Times earlier than the run's start are past midnight."""
    day = service_day + timedelta(days=1) if t < run_start else service_day
    return datetime.combine(day, t)


# How a line is named in answers, as the web page's status line names it. 'central' is the
# main line (CSMT-Kalyan-Kasara/Khopoli).
LINE_NAMES = {"central": "Central line", "harbour": "Harbour line"}
DIRECTION_NAMES = {"down": "away from CSMT", "up": "towards CSMT"}


def line_name(line: str) -> str:
    return LINE_NAMES.get(line, f"{line.capitalize()} line")


@dataclass(frozen=True)
class Journey:
    """A journey with one change: `first` from the origin to `change_at`, then `second`."""

    origin: str  # station codes
    change_at: str
    destination: str
    first: ScheduledTrip
    second: ScheduledTrip


def _covering(con: duckdb.DuckDBPyConnection, first: str, second: str) -> list[tuple[str, str]]:
    """(line, direction) of trains calling at `first` and then at `second`."""
    return con.execute(
        """
        SELECT DISTINCT t.line, t.direction FROM trains t
        JOIN scheduled_stops a ON a.train_id = t.train_id AND a.station_code = ?
        JOIN scheduled_stops b ON b.train_id = t.train_id AND b.station_code = ?
        WHERE a.stop_seq < b.stop_seq
        """,
        [first, second],
    ).fetchall()


def _missing_direction(
    con: duckdb.DuckDBPyConnection, first: str, second: str
) -> tuple[str, str] | None:
    """(line, direction) when trains on a line run from `second` to `first` but that line's
    timetable for the other direction isn't loaded at all, so nothing runs from `first` to
    `second` on it: (harbour, up) for Kurla to Chunabhatti with only the Harbour DOWN PDF."""
    if _covering(con, first, second):
        return None
    for line, direction in _covering(con, second, first):
        wanted = "up" if direction == "down" else "down"
        (count,) = con.execute(
            "SELECT count(*) FROM trains WHERE line = ? AND direction = ?", [line, wanted]
        ).fetchone()
        if not count:
            return line, wanted
    return None


def _not_loaded_text(line: str, direction: str) -> str:
    """'the Harbour line timetable loaded here has no trains towards CSMT (only its DOWN
    timetable is loaded)'."""
    other = "DOWN" if direction == "up" else "UP"
    return (
        f"the {line_name(line)} timetable loaded here has no trains "
        f"{DIRECTION_NAMES[direction]} (only its {other} timetable is loaded)"
    )


def _lines_without_a_direction(con: duckdb.DuckDBPyConnection, lines: set[str]) -> list:
    """(line, direction) for each of `lines` that has trains one way only."""
    out = []
    for line in sorted(lines):
        have = {
            d
            for (d,) in con.execute(
                "SELECT DISTINCT direction FROM trains WHERE line = ?", [line]
            ).fetchall()
        }
        if len(have) == 1:
            out.append((line, "up" if have == {"down"} else "down"))
    return out


def no_train_reason(
    con: duckdb.DuckDBPyConnection,
    origin_code: str,
    destination_code: str,
    when: datetime | None = None,
) -> str | None:
    """The sentence of `no_train_answer`, or None if some train does go there."""
    answer = no_train_answer(con, origin_code, destination_code, when)
    return None if answer is None else answer[0]


def no_train_answer(
    con: duckdb.DuckDBPyConnection,
    origin_code: str,
    destination_code: str,
    when: datetime | None = None,
) -> tuple[str, Journey | None] | None:
    """(why, the journey with one change it names, if any): why no train in the timetable
    goes from `origin_code` to `destination_code`. None if some train does (and there is
    simply none in the time looked at).

    The stations can be on different lines (Kalyan and Panvel: change where the lines
    meet), or the timetable loaded for their line can lack that direction (only the
    Harbour line's DOWN PDF is loaded, so nothing runs towards CSMT on it), or no single
    train calls at both (two branches of one line).

    For stations on different lines, given `when` (naive Mumbai time), it also names the
    quickest journey with one change by the timetable (see `one_change`), unless a part of
    the natural journey runs in a direction whose timetable isn't loaded: then it says so
    rather than send you the long way round.
    """
    from sitt.routes import station_places_sql

    places = station_places_sql(con)
    lines_of = {
        code: {
            line
            for (line,) in con.execute(
                f"SELECT DISTINCT line FROM {places} WHERE code = ?", [code]
            ).fetchall()
        }
        for code in (origin_code, destination_code)
    }
    names = dict(con.execute("SELECT code, name FROM stations").fetchall())
    origin, destination = (
        names.get(origin_code, origin_code),
        names.get(destination_code, destination_code),
    )

    if _covering(con, origin_code, destination_code):
        return None
    journey = None
    shared = lines_of[origin_code] & lines_of[destination_code]
    if not shared:
        meet = [
            code
            for (code,) in con.execute(
                f"""
                SELECT code FROM {places} GROUP BY code
                HAVING bool_or(list_contains(?, line)) AND bool_or(list_contains(?, line))
                ORDER BY min(seq), code
                """,
                [sorted(lines_of[origin_code]), sorted(lines_of[destination_code])],
            ).fetchall()
        ]
        on = (
            f"{origin} is on the {_lines_text(lines_of[origin_code])}, "
            f"{destination} on the {_lines_text(lines_of[destination_code])}"
        )
        meet_names = _and([names.get(code, code) for code in meet]) if meet else ""
        # A part of the journey whose direction isn't loaded: from the origin to where the
        # lines meet, or from there to the destination.
        missing = {
            m
            for code in meet
            for m in (
                _missing_direction(con, origin_code, code),
                _missing_direction(con, code, destination_code),
            )
            if m is not None
        }
        avoid = {code for code in meet if _missing_direction(con, code, destination_code)}
        best = one_change(con, origin_code, destination_code, when, meet, avoid=avoid)
        if best is not None:
            first, change_at, second = best
            journey = Journey(origin_code, change_at, destination_code, first, second)
            after = " after midnight" if second.departure.date() > first.departure.date() else ""
            where = (
                " Quickest with one change on these two lines, by the timetable: the "
                f"{first.departure:%H:%M} {first.train_type}"
                f"{_day_after(first.departure, when)}, changing at "
                f"{names.get(change_at, change_at)} (arrives {first.arrival:%H:%M}) to the "
                f"{second.departure:%H:%M} {second.train_type}{after}, at {destination} "
                f"{second.arrival:%H:%M}. Ask for each part to see delays and crowding."
            )
        elif missing:
            line, direction = sorted(missing)[0]
            where = (
                f" They meet at {meet_names}, but {_not_loaded_text(line, direction)}, so the "
                "part on it can't be worked out yet."
            )
        elif meet:
            where = f" They meet at {meet_names}: ask for each part separately."
        else:
            where = ""
        return (
            f"No train runs from {origin} to {destination} without a change: {on}.{where}",
            journey,
        )
    if (missing := _missing_direction(con, origin_code, destination_code)) is not None:
        line, direction = missing
        text = _not_loaded_text(line, direction)
        return f"{text[0].upper()}{text[1:]}, so none from {origin} to {destination}.", None
    reason = f"No single train in the timetable runs from {origin} to {destination}."
    lacking = _lines_without_a_direction(con, shared)
    if lacking:
        line, direction = lacking[0]
        other = "DOWN" if direction == "up" else "UP"
        reason += (
            f" A journey with a change may need {line_name(line)} trains "
            f"{DIRECTION_NAMES[direction]}, and only its {other} timetable is loaded."
        )
    return reason, None


# Minutes allowed to change trains between lines (a walk across the station, e.g. from the
# main line's platforms at Kurla to the Harbour line's). A guess, not measured.
CHANGE_MINUTES = 5
# How many first trains to try at each interchange.
CHANGE_CANDIDATES = 6
# Longest wait at the change that is still suggested: beyond it, no journey is named.
MAX_WAIT_MINUTES = 60


def one_change(
    con: duckdb.DuckDBPyConnection,
    origin_code: str,
    destination_code: str,
    when: datetime | None,
    interchanges: list[str],
    avoid: set[str] = frozenset(),
) -> tuple[ScheduledTrip, str, ScheduledTrip] | None:
    """The journey with one change at one of `interchanges` that arrives first by the
    timetable, leaving at or after `when`: (first train, where to change, second train).
    At least CHANGE_MINUTES and at most MAX_WAIT_MINUTES between them; ladies' specials are
    left out. Ties go to the one that leaves latest (less waiting). None if `when` is None
    or nothing fits.

    `avoid` are interchanges where the natural change can't be worked out (its onward
    direction isn't loaded). A journey whose first train calls at one of them before its
    own change would be the long way round (Kalyan to Chunabhatti via CSMT instead of
    Kurla), so it isn't offered.
    """
    if when is None:
        return None
    best = None
    for change_at in interchanges:
        if change_at in (origin_code, destination_code) or change_at in avoid:
            continue
        for first in next_trains(con, origin_code, change_at, when, n=CHANGE_CANDIDATES):
            if first.is_ladies_special:
                continue
            if avoid and _calls_between(con, first.train_id, origin_code, change_at) & avoid:
                continue
            seconds = [
                trip
                for trip in next_trains(
                    con,
                    change_at,
                    destination_code,
                    first.arrival + timedelta(minutes=CHANGE_MINUTES),
                    n=3,
                )
                if not trip.is_ladies_special
                and trip.departure - first.arrival <= timedelta(minutes=MAX_WAIT_MINUTES)
            ]
            if not seconds:
                continue
            second = seconds[0]
            key = (second.arrival, -first.departure.timestamp())
            if best is None or key < best[0]:
                best = (key, (first, change_at, second))
    return best[1] if best else None


def _calls_between(con: duckdb.DuckDBPyConnection, train_id: str, a: str, b: str) -> set[str]:
    """Stations `train_id` calls at after `a` and before `b`."""
    return {
        code
        for (code,) in con.execute(
            """
            SELECT s.station_code FROM scheduled_stops s
            WHERE s.train_id = $t
              AND s.stop_seq > (SELECT stop_seq FROM scheduled_stops
                                WHERE train_id = $t AND station_code = $a)
              AND s.stop_seq < (SELECT stop_seq FROM scheduled_stops
                                WHERE train_id = $t AND station_code = $b)
            """,
            {"t": train_id, "a": a, "b": b},
        ).fetchall()
    }


def _day_after(moment: datetime, when: datetime | None) -> str:
    """'' on the day of `when`, ' tomorrow' the day after, else ' on Wednesday'."""
    if when is None or moment.date() == when.date():
        return ""
    if moment.date() == when.date() + timedelta(days=1):
        return " tomorrow"
    return f" on {moment:%A}"


def _lines_text(lines: set[str]) -> str:
    return _and([line_name(line) for line in sorted(lines)])


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
