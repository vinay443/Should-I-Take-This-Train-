"""Open the DuckDB database and apply the schema."""

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


def main() -> None:
    settings = load_settings()
    init_db(settings.db_path).close()
    print(f"Initialised database at {settings.db_path}")


if __name__ == "__main__":
    main()
