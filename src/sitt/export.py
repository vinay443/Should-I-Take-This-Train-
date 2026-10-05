"""Export observations to Parquet, and compact many small Parquet files into monthly ones.

    uv run sitt-export export  [--db data/sitt.duckdb] [--out scratch/export]
    uv run sitt-export compact DIR [--delete-merged] [--include-current]

`export` writes the real observations in the local database as one file per month:

    OUT/observations/2026-10.parquet

`compact` takes a folder of the collector's per-run files
(`2026-10-05/20261005T144504Z-a0f215.parquet`, about a hundred a day) and merges each
finished month into the same monthly layout.

Both are safe to run again: a month is rebuilt from everything there is for it, exact
duplicate rows are dropped, rows are written in a fixed order, and a month whose file is
already up to date is left alone.

What is never written
---------------------
`raw_status`, the source's own text for a train, is not in these files, by construction:
the columns written are `sitt.ingest.live.storage.COLUMNS`, which leaves it out. These
files are meant for a public branch, and Mobond's status text must not be republished
(docs/collector.md). Synthetic observations are not exported either.

Months are UTC months, like the dates in the collector's folder names. A reading taken at
02:00 IST on 1 November belongs to October's file.

Nothing here touches git. docs/collector.md has the steps for putting the files on the
`data` branch by hand.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from dotenv import find_dotenv, load_dotenv

from sitt.config import load_settings
from sitt.db import DatabaseBusyError, open_with_retry
from sitt.ingest.live.storage import COLUMNS

DEFAULT_OUT = Path("scratch") / "export"
OBSERVATIONS_DIR = "observations"
FORBIDDEN_COLUMNS = frozenset({"raw_status"})
SYNTHETIC_SOURCE = "synthetic"

assert not FORBIDDEN_COLUMNS & set(COLUMNS), "raw_status must never be an exported column"

_COLUMN_LIST = ", ".join(COLUMNS)
# A fixed order, so the same rows always give the same file.
_ORDER = "observed_at, source, train_number, station_code, event, batch_id, time_kind"
# epoch_us is UTC, so this is the UTC month whatever the session's time zone is.
_MONTH = "strftime(make_timestamp(epoch_us(observed_at)), '%Y-%m')"


def _quote(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _typed_columns() -> str:
    """SELECT list for the export schema: these names, these types and nothing else."""
    return ", ".join(f"CAST({name} AS {kind}) AS {name}" for name, kind in COLUMNS.items())


def _write_month(con: duckdb.DuckDBPyConnection, month: str, target: Path) -> int:
    """Write the staged rows of `month` to `target`, atomically. Returns the rows written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".parquet.tmp")
    con.execute(
        f"COPY (SELECT {_COLUMN_LIST} FROM staged WHERE month = ? ORDER BY {_ORDER}) "
        f"TO {_quote(temporary)} (FORMAT parquet, COMPRESSION zstd)",
        [month],
    )
    temporary.replace(target)
    return con.execute("SELECT count(*) FROM staged WHERE month = ?", [month]).fetchone()[0]


def _same_rows(con: duckdb.DuckDBPyConnection, month: str, existing: Path) -> bool:
    """Whether `existing` already holds exactly the staged rows of `month`."""
    try:
        columns = {
            row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {_quote(existing)}").fetchall()
        }
        if columns != set(COLUMNS):
            return False
        (differences,) = con.execute(
            f"""
            SELECT count(*) FROM (
                (SELECT {_COLUMN_LIST} FROM staged WHERE month = ?
                 EXCEPT SELECT {_COLUMN_LIST} FROM {_quote(existing)})
                UNION ALL
                (SELECT {_COLUMN_LIST} FROM {_quote(existing)}
                 EXCEPT SELECT {_COLUMN_LIST} FROM staged WHERE month = ?)
            )
            """,
            [month, month],
        ).fetchone()
        (rows,) = con.execute(f"SELECT count(*) FROM {_quote(existing)}").fetchone()
        (staged,) = con.execute("SELECT count(*) FROM staged WHERE month = ?", [month]).fetchone()
    except duckdb.Error:
        return False
    return differences == 0 and rows == staged


@dataclass
class MonthResult:
    month: str
    path: Path
    rows: int
    written: bool  # False when the file was already up to date
    merged: list[Path] = field(default_factory=list)  # small files folded in (compact only)
    duplicates: int = 0  # rows dropped as exact duplicates


def export(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[MonthResult]:
    """Write the real observations in `con` to `out_dir/observations/YYYY-MM.parquet`."""
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE staged AS
        SELECT DISTINCT {_typed_columns()}, {_MONTH} AS month
        FROM observations WHERE source <> ?
        """,
        [SYNTHETIC_SOURCE],
    )
    (total,) = con.execute(
        "SELECT count(*) FROM observations WHERE source <> ?", [SYNTHETIC_SOURCE]
    ).fetchone()
    months = [
        row[0] for row in con.execute("SELECT DISTINCT month FROM staged ORDER BY 1").fetchall()
    ]
    results = []
    for month in months:
        target = Path(out_dir) / OBSERVATIONS_DIR / f"{month}.parquet"
        if target.is_file() and _same_rows(con, month, target):
            (rows,) = con.execute("SELECT count(*) FROM staged WHERE month = ?", [month]).fetchone()
            results.append(MonthResult(month, target, rows, written=False))
        else:
            results.append(MonthResult(month, target, _write_month(con, month, target), True))
    if results:
        results[0].duplicates = total - sum(result.rows for result in results)
    return results


def _month_of_file(path: Path, root: Path) -> str | None:
    """The month a collector file belongs to, from its folder or name; None if it isn't one."""
    relative = path.relative_to(root)
    if len(relative.parts) == 1 and len(path.stem) == 7 and path.stem[4] == "-":
        return path.stem  # a monthly file: 2026-10.parquet
    folder = relative.parts[0]
    if len(relative.parts) == 2 and len(folder) == 10 and folder[4] == "-" and folder[7] == "-":
        return folder[:7]  # a per-run file: 2026-10-05/<batch>.parquet
    return None


def compact(
    observations_dir: Path,
    *,
    delete_merged: bool = False,
    include_current: bool = False,
    now: datetime | None = None,
) -> list[MonthResult]:
    """Merge the per-run Parquet files under `observations_dir` into one file per month.

    Only months that are over are compacted, unless `include_current`: the collector is
    still adding files to this month. The small files that were merged are removed only
    with `delete_merged`, and only after the monthly file has been written and read back
    with every one of their rows in it.
    """
    root = Path(observations_dir)
    now = now or datetime.now(UTC)
    current = f"{now.astimezone(UTC):%Y-%m}"
    by_month: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*.parquet")):
        month = _month_of_file(path, root)
        if month is not None and (include_current or month < current):
            by_month.setdefault(month, []).append(path)

    results = []
    for month, paths in sorted(by_month.items()):
        target = root / f"{month}.parquet"
        small = [path for path in paths if path != target]
        with duckdb.connect() as con:
            files = [path.as_posix() for path in paths]
            # Columns are picked by name, so a file with extra columns (raw_status, say)
            # contributes only the ones that are meant to be published.
            con.execute(
                f"""
                CREATE TEMP TABLE staged AS
                SELECT DISTINCT {_typed_columns()}, ? AS month
                FROM read_parquet(?, union_by_name = true) AS files
                WHERE source <> ?
                """,
                [month, files, SYNTHETIC_SOURCE],
            )
            (read,) = con.execute(
                "SELECT count(*) FROM read_parquet(?, union_by_name = true) WHERE source <> ?",
                [files, SYNTHETIC_SOURCE],
            ).fetchone()
            (rows,) = con.execute("SELECT count(*) FROM staged").fetchone()
            up_to_date = target.is_file() and _same_rows(con, month, target)
            if not up_to_date:
                _write_month(con, month, target)
                if not _same_rows(con, month, target):
                    raise RuntimeError(f"{target} did not read back with the rows written to it")
        result = MonthResult(month, target, rows, not up_to_date, small, read - rows)
        results.append(result)
        if delete_merged:
            for path in small:
                path.unlink()
            for folder in sorted({path.parent for path in small}):
                if folder != root and not any(folder.iterdir()):
                    folder.rmdir()
    return results


def parquet_columns(path: Path) -> list[str]:
    with duckdb.connect() as con:
        return [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {_quote(path)}").fetchall()]


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sitt-export",
        description="Export observations to monthly Parquet files, or compact a folder of "
        "the collector's per-run files into monthly ones. raw_status is never written.",
        epilog="Nothing here touches git. See docs/collector.md.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export_parser = commands.add_parser(
        "export", help="write the local database's real observations, one file per month"
    )
    export_parser.add_argument("--db", type=Path, help="default: SITT_DB_PATH or data/sitt.duckdb")
    export_parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"folder to write observations/YYYY-MM.parquet in (default {DEFAULT_OUT.as_posix()})",
    )
    compact_parser = commands.add_parser(
        "compact", help="merge per-run Parquet files into one file per finished month"
    )
    compact_parser.add_argument("dir", type=Path, help="the observations folder to compact")
    compact_parser.add_argument(
        "--delete-merged",
        action="store_true",
        help="remove the small files once their rows are safely in the monthly file",
    )
    compact_parser.add_argument(
        "--include-current",
        action="store_true",
        help="also compact the current month (don't, while a collector is writing to it)",
    )
    args = parser.parse_args(argv)

    if args.command == "export":
        load_dotenv(find_dotenv(usecwd=True))
        db_path = args.db or load_settings().db_path
        try:
            with open_with_retry(db_path, read_only=True) as con:
                results = export(con, args.out)
        except FileNotFoundError as exc:
            print(f"{exc}. Run `uv run sitt-collect` first.", file=sys.stderr)
            return 2
        except DatabaseBusyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not results:
            print("No real observations to export.")
            return 0
        for result in results:
            state = "written" if result.written else "already up to date"
            print(f"{result.path}: {result.rows:,} rows, {state}")
        dropped = sum(result.duplicates for result in results)
        if dropped:
            print(f"{dropped:,} exact duplicate row(s) were left out.")
        print("raw_status is not in these files.")
        return 0

    if not args.dir.is_dir():
        print(f"No such folder: {args.dir}", file=sys.stderr)
        return 2
    results = compact(
        args.dir, delete_merged=args.delete_merged, include_current=args.include_current, now=now
    )
    if not results:
        print("Nothing to compact: no finished month has files here.")
        return 0
    for result in results:
        state = "written" if result.written else "already up to date"
        tail = f"; {result.duplicates:,} duplicate row(s) dropped" if result.duplicates else ""
        print(
            f"{result.path}: {result.rows:,} rows from {len(result.merged)} small file(s), "
            f"{state}{tail}"
        )
    small = sum(len(result.merged) for result in results)
    if small and args.delete_merged:
        print(f"Removed {small} small file(s).")
    elif small:
        print(f"{small} small file(s) kept. Run again with --delete-merged to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
