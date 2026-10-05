"""Open the DuckDB database and apply the schema."""

import time
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

import duckdb

from sitt.config import load_settings

SCHEMA_SQL = files("sitt.db").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Connect to the database at `path`, or at the configured path if `path` is None."""
    db_path = Path(path) if path is not None else load_settings().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def init_db(path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Create any missing tables and return an open connection. Safe to call repeatedly."""
    con = connect(path)
    con.execute(SCHEMA_SQL)
    return con


class DatabaseBusyError(RuntimeError):
    """The database file could not be opened, most likely because another process has it."""


def open_with_retry(
    path: Path | str,
    *,
    read_only: bool = False,
    attempts: int = 5,
    wait_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> duckdb.DuckDBPyConnection:
    """Open the database, waiting out another process that holds it.

    DuckDB allows one writing process at a time, and on Windows a writer also shuts out
    readers. The collector takes the file for a second or two every 15 minutes, so a short
    wait normally gets through. `read_only` opens an existing file without applying the
    schema; otherwise this is `init_db`. Waits grow by half each time.

    Raises DatabaseBusyError, with a message fit to show a person, when every attempt fails,
    and FileNotFoundError when a read-only open is asked of a file that doesn't exist.
    """
    db_path = Path(path)
    if read_only and not db_path.is_file():
        raise FileNotFoundError(f"No database at {db_path}")
    wait = wait_seconds
    for attempt in range(1, attempts + 1):
        try:
            if read_only:
                return duckdb.connect(str(db_path), read_only=True)
            return init_db(db_path)
        except duckdb.IOException as exc:
            if attempt == attempts:
                raise DatabaseBusyError(
                    f"{db_path} is in use by another process (the collector, the bot or the "
                    f"dashboard) and stayed busy for {attempts} attempts. Try again in a "
                    "minute."
                ) from exc
            sleep(wait)
            wait *= 1.5
    raise AssertionError("unreachable")


def main() -> None:
    settings = load_settings()
    init_db(settings.db_path).close()
    print(f"Initialised database at {settings.db_path}")


if __name__ == "__main__":
    main()
