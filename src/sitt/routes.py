"""Where each train is scheduled to be, station by station, including stations it passes.

`scheduled_stops` only has the stations a train calls at. Live sources also report
trains at stations they pass ("Crossed MATUNGA" for a fast train), so both the synthetic
generator and the model features need a scheduled time at those stations too.

Between CSMT and Kalyan the main line is a single run of stations, so the pass time at a
skipped station is interpolated between the stops either side, in proportion to the
number of stations. Beyond Kalyan the line branches and trains call at every station, so
no points are added there. The same goes for every other line (the Harbour line's
trains call everywhere): only main line trains get points added.

A station's place (`station_seq`) is its place on the train's own line, from
`station_lines`: CSMT is 1 on both lines, and Dockyard Road (4th on the Harbour line) is
never confused with Byculla (4th on the main line).
"""

from dataclasses import dataclass
from datetime import time

import duckdb

from sitt.ingest.timetable import _stage

# The far end of the main line's unbranched section that starts at seq 1 (CSMT).
CORE_LINE = "central"
CORE_END_STATION = "KYN"

ROUTE_POINT_COLUMNS = {
    "train_id": "VARCHAR",
    "station_code": "VARCHAR",
    "point_seq": "INTEGER",
    "station_seq": "INTEGER",
    "minutes": "DOUBLE",
    "is_stop": "BOOLEAN",
    "progress": "DOUBLE",
}


@dataclass(frozen=True)
class RoutePoint:
    station_code: str
    station_seq: int  # station_lines.seq: place on the train's line from the CSMT end
    minutes: float  # scheduled minutes after the train leaves its first station
    is_stop: bool


@dataclass(frozen=True)
class Route:
    train_id: str
    number: str
    direction: str
    train_type: str
    line: str
    run_start: time  # scheduled departure from the first station
    days: str  # Monday-to-Sunday Y/N mask of the first stop
    points: tuple[RoutePoint, ...]

    @property
    def duration(self) -> float:
        return self.points[-1].minutes


def _minutes(t: time) -> float:
    return t.hour * 60 + t.minute + t.second / 60


def load_routes(con: duckdb.DuckDBPyConnection, line: str | None = None) -> list[Route]:
    """Every train's route points, in travel order."""
    line_seqs = line_station_seqs(con)
    main = line_seqs.get(CORE_LINE, {})
    core_end = main.get(CORE_END_STATION, 0)
    core = {seq: code for code, seq in main.items() if seq <= core_end}
    rows = con.execute(
        """
        SELECT t.train_id, t.number, t.direction, t.train_type, t.line,
               list(s.station_code ORDER BY s.stop_seq),
               list(coalesce(s.scheduled_departure, s.scheduled_arrival) ORDER BY s.stop_seq),
               first(s.days_of_operation ORDER BY s.stop_seq)
        FROM trains t JOIN scheduled_stops s USING (train_id)
        WHERE $line IS NULL OR t.line = $line
        GROUP BY ALL ORDER BY t.train_id
        """,
        {"line": line},
    ).fetchall()

    routes = []
    for train_id, number, direction, train_type, train_line, codes, times, days in rows:
        seqs = line_seqs[train_line]
        interpolate = train_line == CORE_LINE
        start = _minutes(times[0])
        minutes = []
        for t in times:  # times wrap past midnight; they only ever increase along a route
            value = (_minutes(t) - start) % 1440
            minutes.append(value)
        points = [RoutePoint(codes[0], seqs[codes[0]], 0.0, True)]
        for i in range(1, len(codes)):
            a, b = seqs[codes[i - 1]], seqs[codes[i]]
            if interpolate and a in core and b in core and abs(b - a) > 1:
                step = 1 if b > a else -1
                gap = abs(b - a)
                for k in range(1, gap):
                    seq = a + k * step
                    at = minutes[i - 1] + (minutes[i] - minutes[i - 1]) * k / gap
                    points.append(RoutePoint(core[seq], seq, at, False))
            points.append(RoutePoint(codes[i], b, minutes[i], True))
        routes.append(
            Route(
                train_id, number, direction, train_type, train_line, times[0], days, tuple(points)
            )
        )
    return routes


def station_places_sql(con: duckdb.DuckDBPyConnection) -> str:
    """A FROM item with (code, line, seq): every line each station is on and its place.

    That is `station_lines`. A database it hasn't reached yet (made before it existed and
    not opened for writing since, or written without the timetable loader) has only
    `stations`, with one line per station, and that is used instead.
    """
    filled = (
        con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'station_lines'"
        ).fetchone()[0]
        and con.execute("SELECT count(*) FROM station_lines").fetchone()[0]
    )
    return "station_lines" if filled else "(SELECT code, line, seq FROM stations)"


def line_station_seqs(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, int]]:
    """{line: {station code: place on that line}} (see station_places_sql)."""
    out: dict[str, dict[str, int]] = {}
    rows = con.execute(f"SELECT code, line, seq FROM {station_places_sql(con)}").fetchall()
    for code, line, seq in rows:
        out.setdefault(line, {})[code] = seq
    return out


def stage_route_points(con: duckdb.DuckDBPyConnection, routes: list[Route] | None = None) -> None:
    """Create the temporary table `route_points` (see ROUTE_POINT_COLUMNS).

    `progress` runs from 0 at the first station to 1 at the last, by scheduled time.
    """
    routes = load_routes(con) if routes is None else routes
    rows = [
        {
            "train_id": route.train_id,
            "station_code": point.station_code,
            "point_seq": i,
            "station_seq": point.station_seq,
            "minutes": point.minutes,
            "is_stop": point.is_stop,
            "progress": point.minutes / route.duration if route.duration else 0.0,
        }
        for route in routes
        for i, point in enumerate(route.points)
    ]
    _stage(con, "route_points", ROUTE_POINT_COLUMNS, rows)
