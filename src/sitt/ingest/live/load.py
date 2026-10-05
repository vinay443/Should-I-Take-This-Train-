"""Import collected Parquet batches into the local DuckDB `observations` table.

    python -m sitt.ingest.live.load [--dir data/observations] [--db data/sitt.duckdb]

Safe to re-run: batches already in the table (by `batch_id`) are skipped. After loading
it re-matches raw train numbers and station names against `trains` and `stations`, so
run it again after loading a new timetable.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from sitt.config import load_settings
from sitt.db import init_db
from sitt.ingest.live.storage import COLUMNS


@dataclass(frozen=True)
class LoadResult:
    files: int
    rows: int
    trains_matched: int
    stations_matched: int


def load_observations(con: duckdb.DuckDBPyConnection, observations_dir: Path) -> tuple[int, int]:
    """Insert rows from Parquet files whose batch isn't loaded yet. Returns (files, rows)."""
    loaded = {
        batch
        for (batch,) in con.execute(
            "SELECT DISTINCT batch_id FROM observations WHERE batch_id IS NOT NULL"
        ).fetchall()
    }
    files = [
        path.as_posix()
        for path in sorted(Path(observations_dir).rglob("*.parquet"))
        if path.stem not in loaded
    ]
    if not files:
        return 0, 0
    columns = ", ".join(COLUMNS)
    (rows,) = con.execute(
        f"INSERT INTO observations ({columns}) "
        f"SELECT {columns} FROM read_parquet(?, union_by_name = true) "
        "WHERE batch_id NOT IN "
        "(SELECT batch_id FROM observations WHERE batch_id IS NOT NULL)",
        [files],
    ).fetchone()
    return len(files), rows


def rematch(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Point raw train numbers and station names at known trains/stations where unambiguous.

    Returns (trains matched, stations matched) in this pass.
    """
    (trains,) = con.execute(
        """
        UPDATE observations SET train_id = m.train_id
        FROM (SELECT number, min(train_id) AS train_id FROM trains
              GROUP BY number HAVING count(*) = 1) m
        WHERE observations.train_number = m.number
          AND observations.train_id = observations.train_number
        """
    ).fetchone()
    # Western Railway trains (90-94xxx) are left alone: WR's Dadar isn't Central's Dadar.
    (stations,) = con.execute(
        """
        UPDATE observations SET station_code = m.code
        FROM (SELECT upper(regexp_replace(name, '[^A-Za-z]', '', 'g')) AS key,
                     min(code) AS code
              FROM stations GROUP BY key HAVING count(*) = 1) m
        WHERE upper(regexp_replace(observations.station_code, '[^A-Za-z]', '', 'g')) = m.key
          AND observations.station_code <> ''
          AND observations.station_code NOT IN (SELECT code FROM stations)
          AND NOT regexp_matches(observations.train_number, '^9[0-4]')
        """
    ).fetchone()
    return trains, stations


def load(con: duckdb.DuckDBPyConnection, observations_dir: Path) -> LoadResult:
    files, rows = load_observations(con, observations_dir)
    trains, stations = rematch(con)
    return LoadResult(files, rows, trains, stations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sitt.ingest.live.load", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--dir", type=Path, default=Path("data/observations"))
    parser.add_argument("--db", type=Path, help="default: SITT_DB_PATH or data/sitt.duckdb")
    args = parser.parse_args(argv)

    if not args.dir.is_dir():
        print(f"No such directory: {args.dir}", file=sys.stderr)
        return 1
    db_path = args.db or load_settings().db_path
    with init_db(db_path) as con:
        result = load(con, args.dir)
    print(
        f"Loaded {result.rows} rows from {result.files} new file(s) into {db_path}; "
        f"matched {result.trains_matched} train and {result.stations_matched} station reference(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
