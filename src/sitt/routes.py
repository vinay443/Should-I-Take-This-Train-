"""Where each train is scheduled to be, station by station, including stations it passes.

`scheduled_stops` only has the stations a train calls at. Live sources also report
trains at stations they pass ("Crossed MATUNGA" for a fast train), so both the synthetic
generator and the model features need a scheduled time at those stations too.

Between CSMT and Kalyan the line is a single run of stations, so the pass time at a
skipped station is interpolated between the stops either side, in proportion to the
number of stations. Beyond Kalyan the line branches and trains call at every station, so
no points are added there.
"""

from dataclasses import dataclass
from datetime import time

import duckdb

from sitt.ingest.timetable import _stage

# The far end of the unbranched section that starts at seq 1 (CSMT).
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
    station_seq: int  # stations.seq: position along the line from the CSMT end
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
    seqs = dict(con.execute("SELECT code, seq FROM stations").fetchall())
    core_end = seqs.get(CORE_END_STATION, 0)
    core = {seq: code for code, seq in seqs.items() if seq <= core_end}
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
        start = _minutes(times[0])
        minutes = []
        for t in times:  # times wrap past midnight; they only ever increase along a route
            value = (_minutes(t) - start) % 1440
            minutes.append(value)
        points = [RoutePoint(codes[0], seqs[codes[0]], 0.0, True)]
        for i in range(1, len(codes)):
            a, b = seqs[codes[i - 1]], seqs[codes[i]]
            if a in core and b in core and abs(b - a) > 1:
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
