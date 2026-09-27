"""Load a normalised timetable CSV into `stations`, `trains` and `scheduled_stops`.

The CSV format is documented in docs/timetable-format.md. Adapters for real data
sources should convert to that format rather than write to the database, so this
module stays the only place that knows how the timetable tables are filled.

    python -m sitt.ingest.timetable path/to/timetable.csv [--line central] [--replace]

Loading is idempotent: re-running a file leaves the database exactly as the first
run did. A train in the file replaces any earlier version of the same train.
"""

import argparse
import csv
import heapq
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import time
from itertools import pairwise
from pathlib import Path

import duckdb

from sitt.db import init_db

DEFAULT_LINE = "central"

REQUIRED_COLUMNS = (
    "train_number",
    "destination",
    "service_type",
    "direction",
    "station_code",
    "station_name",
    "scheduled_arrival",
    "scheduled_departure",
    "days",
)
# Optional: overrides the default train_id of "<line>-<train_number>".
TRAIN_ID_COLUMN = "train_id"

SERVICE_TYPES = ("fast", "slow")
DIRECTIONS = ("up", "down")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EVERY_DAY = "YYYYYYY"

# Consecutive times on a train's route more than this far apart are treated as
# out of order (e.g. 07:10 listed after 07:15 would otherwise read as a 23h55m hop).
MAX_STEP_SECONDS = 2 * 3600
MAX_RUN_SECONDS = 12 * 3600
DAY_SECONDS = 24 * 3600
MAX_ERRORS_SHOWN = 50

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


class TimetableError(ValueError):
    """The timetable is invalid. `errors` lists every problem found."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        shown = self.errors[:MAX_ERRORS_SHOWN]
        more = len(self.errors) - len(shown)
        lines = [f"{len(self.errors)} problem(s) in timetable:", *(f"  {e}" for e in shown)]
        if more:
            lines.append(f"  ... and {more} more")
        super().__init__("\n".join(lines))


@dataclass
class Stop:
    line_no: int
    station_code: str
    station_name: str
    arrival: time | None
    departure: time | None
    days: str


@dataclass
class Train:
    train_id: str
    number: str
    label: str
    train_type: str
    direction: str
    stops: list[Stop] = field(default_factory=list)


@dataclass
class Timetable:
    line: str
    trains: list[Train]

    @property
    def stations(self) -> dict[str, str]:
        return {s.station_code: s.station_name for t in self.trains for s in t.stops}


@dataclass(frozen=True)
class LoadResult:
    trains: int
    stops: int
    stations: int
    removed_trains: int


def parse_time(text: str) -> time | None:
    """Parse 'HH:MM' or 'HH:MM:SS' (24-hour clock). Blank means no time."""
    value = text.strip()
    if not value:
        return None
    match = _TIME_RE.fullmatch(value)
    if match:
        hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
        if hour <= 23 and minute <= 59 and second <= 59:
            return time(hour, minute, second)
    raise ValueError(f"invalid time {text!r}, expected HH:MM between 00:00 and 23:59")


def parse_days(text: str) -> str:
    """Parse a running-days value into the schema's Monday-to-Sunday 'YYYYYYN' mask.

    Accepts blank or 'daily', a mask such as 'YYYYYYN', or day names and ranges
    joined by '|', such as 'mon-sat' or 'sat|sun'.
    """
    value = text.strip().lower()
    if value in ("", "daily"):
        return EVERY_DAY
    if len(value) == 7 and set(value) <= {"y", "n"}:
        mask = value.upper()
    else:
        runs = [False] * 7
        for token in value.split("|"):
            first, dash, last = (part.strip() for part in token.partition("-"))
            if first not in DAY_NAMES or (dash and last not in DAY_NAMES):
                raise ValueError(
                    f"invalid days {text!r}, expected 'daily', a mask like 'YYYYYYN', "
                    "or days like 'mon-sat' or 'sat|sun'"
                )
            day, end = DAY_NAMES.index(first), DAY_NAMES.index(last or first)
            runs[day] = True
            while day != end:
                day = (day + 1) % 7
                runs[day] = True
        mask = "".join("Y" if run else "N" for run in runs)
    if "Y" not in mask:
        raise ValueError(f"invalid days {text!r}: train never runs")
    return mask


def _data_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line number, text) for each line that is not blank or a '#' comment."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        for line_no, text in enumerate(f, start=1):
            if text.strip() and not text.lstrip().startswith("#"):
                yield line_no, text


def read_timetable(path: str | Path, *, line: str = DEFAULT_LINE) -> Timetable:
    """Read and validate a timetable CSV. Raises TimetableError listing every problem."""
    path = Path(path)
    lines = _data_lines(path)
    header_line = next(lines, None)
    if header_line is None:
        raise TimetableError([f"{path}: file has no header row"])
    header = [name.strip().lower() for name in next(csv.reader([header_line[1]]))]
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    duplicated = sorted({name for name in header if header.count(name) > 1})
    if missing or duplicated:
        raise TimetableError(
            [f"line {header_line[0]}: missing column {name!r}" for name in missing]
            + [f"line {header_line[0]}: duplicate column {name!r}" for name in duplicated]
        )

    errors: list[str] = []
    trains: dict[str, Train] = {}
    station_names: dict[str, tuple[str, int]] = {}
    # Trains with a row that could not be parsed skip the whole-train checks,
    # which would only report knock-on effects of the missing row.
    broken_trains: set[str] = set()

    for line_no, text in lines:
        values = next(csv.reader([text]))
        if len(values) != len(header):
            errors.append(f"line {line_no}: expected {len(header)} fields, got {len(values)}")
            continue
        row = {name: value.strip() for name, value in zip(header, values, strict=True)}
        row_errors: list[str] = []

        for name in ("train_number", "destination", "station_code", "station_name"):
            if not row[name]:
                row_errors.append(f"{name} is blank")
        service_type = row["service_type"].lower()
        if service_type not in SERVICE_TYPES:
            row_errors.append(f"service_type {row['service_type']!r} is not 'fast' or 'slow'")
        direction = row["direction"].lower()
        if direction not in DIRECTIONS:
            row_errors.append(f"direction {row['direction']!r} is not 'up' or 'down'")
        parsed = {}
        for name, parse in (
            ("scheduled_arrival", parse_time),
            ("scheduled_departure", parse_time),
            ("days", parse_days),
        ):
            try:
                parsed[name] = parse(row[name])
            except ValueError as e:
                row_errors.append(f"{name}: {e}")
        code, number = row["station_code"].upper(), row["train_number"]
        train_id = row.get(TRAIN_ID_COLUMN) or f"{line}-{number}"
        if row_errors:
            errors.extend(f"line {line_no}: {e}" for e in row_errors)
            broken_trains.add(train_id)
            continue

        stop = Stop(
            line_no=line_no,
            station_code=code,
            station_name=row["station_name"],
            arrival=parsed["scheduled_arrival"],
            departure=parsed["scheduled_departure"],
            days=parsed["days"],
        )

        known_name, first_line = station_names.setdefault(code, (stop.station_name, line_no))
        if known_name != stop.station_name:
            errors.append(
                f"line {line_no}: station {code} is named {stop.station_name!r} here "
                f"but {known_name!r} on line {first_line}"
            )

        train = trains.get(train_id)
        if train is None:
            train = trains[train_id] = Train(
                train_id, number, row["destination"], service_type, direction
            )
        else:
            for name, expected, actual in (
                ("train_number", train.number, number),
                ("destination", train.label, row["destination"]),
                ("service_type", train.train_type, service_type),
                ("direction", train.direction, direction),
            ):
                if expected != actual:
                    errors.append(
                        f"line {line_no}: train {train_id} has {name} {actual!r} here "
                        f"but {expected!r} on line {train.stops[0].line_no}"
                    )
            if any(s.station_code == code for s in train.stops):
                errors.append(f"line {line_no}: train {train_id} stops at {code} more than once")
                continue
        train.stops.append(stop)

    if not trains and not errors:
        errors.append(f"{path}: no trains in file")
    complete = [t for t in trains.values() if t.train_id not in broken_trains]
    for train in complete:
        errors.extend(_check_train(train))
    try:
        station_order((t.number, t.direction, [s.station_code for s in t.stops]) for t in complete)
    except TimetableError as e:
        errors.extend(e.errors)

    if errors:
        raise TimetableError(errors)
    return Timetable(line=line, trains=list(trains.values()))


def _check_train(train: Train) -> list[str]:
    """Check a train's stops have the times they need, in increasing order along its route."""
    stops = train.stops
    where = f"train {train.train_id}"
    if len(stops) < 2:
        return [f"line {stops[0].line_no}: {where} has only one stop"]

    errors = []
    for i, stop in enumerate(stops):
        prefix = f"line {stop.line_no}: {where} at {stop.station_code}"
        if i == 0 and stop.departure is None:
            errors.append(f"{prefix}: first stop needs a scheduled_departure")
        elif i == len(stops) - 1 and stop.arrival is None:
            errors.append(f"{prefix}: last stop needs a scheduled_arrival")
        elif stop.arrival is None and stop.departure is None:
            errors.append(f"{prefix}: needs a scheduled_arrival or scheduled_departure")

    # Times may wrap past midnight, so compare each time with the one before it.
    points = [
        (t, f"{kind} at {stop.station_code}", stop.line_no)
        for stop in stops
        for kind, t in (("arrival", stop.arrival), ("departure", stop.departure))
        if t is not None
    ]
    run = 0
    for (prev, prev_what, _), (cur, what, line_no) in pairwise(points):
        step = (_seconds(cur) - _seconds(prev)) % DAY_SECONDS
        if step > MAX_STEP_SECONDS:
            errors.append(
                f"line {line_no}: {where}: {what} {cur:%H:%M} does not follow "
                f"{prev_what} {prev:%H:%M}; stops must be in route order with times "
                "increasing along the route"
            )
        run += step
    if run >= MAX_RUN_SECONDS:
        errors.append(f"line {stops[0].line_no}: {where} runs for 12 hours or more")
    return errors


def _seconds(t: time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def station_order(routes: Iterable[tuple[str, str, Sequence[str]]]) -> list[str]:
    """Order a line's stations in the down direction, starting from the CSMT end.

    `routes` holds (train number, direction, station codes in travel order) for
    each train. Up trains run the order in reverse. Raises TimetableError if the
    trains disagree about the order. Stations that no train orders relative to
    each other (e.g. on different branches) are ordered by code.
    """
    errors = []
    first_seen: dict[tuple[str, str], str] = {}
    following: dict[str, set[str]] = defaultdict(set)
    preceding_count: dict[str, int] = {}

    for number, direction, codes in routes:
        down = list(codes) if direction == "down" else list(reversed(codes))
        conflict = None
        for i, a in enumerate(down):
            preceding_count.setdefault(a, 0)
            for b in down[i + 1 :]:
                if conflict is None and (b, a) in first_seen:
                    conflict = (a, b, first_seen[(b, a)])
                first_seen.setdefault((a, b), number)
        if conflict:
            a, b, other = conflict
            errors.append(
                f"train {number} ({direction}) disagrees with train {other} about station order: "
                f"it puts {a} before {b} in the down direction (is its direction right?)"
            )
        for a, b in pairwise(down):
            if b not in following[a]:
                following[a].add(b)
                preceding_count[b] = preceding_count.get(b, 0) + 1

    if errors:
        raise TimetableError(errors)

    ready = [code for code, count in preceding_count.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        code = heapq.heappop(ready)
        order.append(code)
        for nxt in following[code]:
            preceding_count[nxt] -= 1
            if preceding_count[nxt] == 0:
                heapq.heappush(ready, nxt)
    if len(order) < len(preceding_count):
        stuck = sorted(set(preceding_count) - set(order))
        raise TimetableError([f"station order is circular among: {', '.join(stuck)}"])
    return order


# Column types of the temporary tables a load is staged in (see _stage).
_STATION_COLUMNS = {"code": "VARCHAR", "name": "VARCHAR"}
_TRAIN_COLUMNS = dict.fromkeys(
    ("train_id", "number", "label", "train_type", "direction"), "VARCHAR"
)
_STOP_COLUMNS = {
    "train_id": "VARCHAR",
    "station_code": "VARCHAR",
    "stop_seq": "INTEGER",
    "arrival": "TIME",
    "departure": "TIME",
    "days": "VARCHAR",
}


def load_timetable(
    con: duckdb.DuckDBPyConnection, timetable: Timetable, *, replace: bool = False
) -> LoadResult:
    """Write a validated timetable to the database in a single transaction.

    Each train in the timetable replaces any stored train with the same train_id.
    With `replace`, the line's trains that are not in the timetable are removed too.
    Station `seq` is recomputed from every train on the line after the load.
    """
    line = timetable.line
    trains = timetable.trains
    stations = timetable.stations
    stops = [
        {
            "train_id": train.train_id,
            "station_code": stop.station_code,
            "stop_seq": seq,
            "arrival": stop.arrival and stop.arrival.isoformat(),
            "departure": stop.departure and stop.departure.isoformat(),
            "days": stop.days,
        }
        for train in trains
        for seq, stop in enumerate(train.stops, start=1)
    ]

    con.begin()
    try:
        _stage(
            con,
            "load_stations",
            _STATION_COLUMNS,
            [{"code": code, "name": name} for code, name in stations.items()],
        )
        _stage(
            con,
            "load_trains",
            _TRAIN_COLUMNS,
            [{column: getattr(t, column) for column in _TRAIN_COLUMNS} for t in trains],
        )
        _stage(con, "load_stops", _STOP_COLUMNS, stops)

        removed = 0
        if replace:
            (removed,) = con.execute(
                """
                SELECT count(*) FROM trains
                WHERE line = ? AND train_id NOT IN (SELECT train_id FROM load_trains)
                """,
                [line],
            ).fetchone()
            con.execute(
                """
                DELETE FROM scheduled_stops WHERE train_id IN (
                    SELECT train_id FROM trains
                    WHERE line = ? AND train_id NOT IN (SELECT train_id FROM load_trains))
                """,
                [line],
            )
        con.execute(
            "DELETE FROM scheduled_stops WHERE train_id IN (SELECT train_id FROM load_trains)"
        )
        con.execute(
            """
            INSERT INTO stations (code, name, line, seq)
            SELECT code, name, ?, 0 FROM load_stations
            ON CONFLICT (code) DO UPDATE SET name = excluded.name, line = excluded.line
            """,
            [line],
        )
        con.execute(
            """
            INSERT INTO trains (train_id, number, label, train_type, line, direction)
            SELECT train_id, number, label, train_type, ?, direction FROM load_trains
            ON CONFLICT (train_id) DO UPDATE SET
                number = excluded.number, label = excluded.label,
                train_type = excluded.train_type, line = excluded.line,
                direction = excluded.direction
            """,
            [line],
        )
        con.execute(
            """
            INSERT INTO scheduled_stops (train_id, station_code, stop_seq, scheduled_arrival,
                                         scheduled_departure, days_of_operation)
            SELECT train_id, station_code, stop_seq, arrival, departure, days FROM load_stops
            """
        )
        _update_station_seq(con, line)
        for table in ("load_stations", "load_trains", "load_stops", "load_station_seq"):
            con.execute(f"DROP TABLE {table}")
        con.commit()
    except BaseException:
        con.rollback()
        raise

    if removed:
        # DuckDB rejects deleting a train in the same transaction as its stops
        # (a foreign key limitation), so drop the now stop-less trains separately.
        # If this fails, the leftover trains have no stops and are invisible to
        # timetable queries; the next --replace load removes them.
        con.execute(
            """
            DELETE FROM trains WHERE line = ?
              AND train_id NOT IN (SELECT DISTINCT train_id FROM scheduled_stops)
            """,
            [line],
        )
    return LoadResult(
        trains=len(trains), stops=len(stops), stations=len(stations), removed_trains=removed
    )


def _stage(
    con: duckdb.DuckDBPyConnection, table: str, columns: dict[str, str], rows: list[dict]
) -> None:
    """Load rows into a temporary table, passed to DuckDB as a single JSON string.

    Passing Python lists as parameters is far slower: DuckDB converts each value
    separately and, when pandas is not installed, retries importing it every time.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {table} AS
        SELECT unnest(from_json($rows, '{json.dumps([columns])}'), recursive := true)
        """,
        {"rows": json.dumps(rows)},
    )


def _update_station_seq(con: duckdb.DuckDBPyConnection, line: str) -> None:
    routes = con.execute(
        """
        SELECT t.number, t.direction, list(s.station_code ORDER BY s.stop_seq)
        FROM trains t JOIN scheduled_stops s USING (train_id)
        WHERE t.line = ?
        GROUP BY t.train_id, t.number, t.direction
        ORDER BY t.train_id
        """,
        [line],
    ).fetchall()
    try:
        order = station_order(routes)
    except TimetableError as e:
        raise TimetableError(
            [f"conflicts with trains already loaded for line {line!r}: {msg}" for msg in e.errors]
        ) from None
    _stage(
        con,
        "load_station_seq",
        {"code": "VARCHAR", "seq": "INTEGER"},
        [{"code": code, "seq": seq} for seq, code in enumerate(order, start=1)],
    )
    con.execute(
        "UPDATE stations SET seq = o.seq FROM load_station_seq o WHERE stations.code = o.code"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sitt.ingest.timetable",
        description="Load a timetable CSV (see docs/timetable-format.md) into the database.",
    )
    parser.add_argument("csv", type=Path, help="timetable CSV file")
    parser.add_argument(
        "--line", default=DEFAULT_LINE, help=f"line the timetable is for (default: {DEFAULT_LINE})"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="also delete this line's trains that are not in the file",
    )
    parser.add_argument(
        "--db", type=Path, help="database file (default: SITT_DB_PATH or data/sitt.duckdb)"
    )
    args = parser.parse_args(argv)

    try:
        timetable = read_timetable(args.csv, line=args.line)
        with init_db(args.db) as con:
            result = load_timetable(con, timetable, replace=args.replace)
    except (TimetableError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(
        f"Loaded {result.trains} trains, {result.stops} stops and {result.stations} stations "
        f"for line {args.line!r}."
    )
    if result.removed_trains:
        print(f"Removed {result.removed_trains} trains that are no longer in the timetable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
