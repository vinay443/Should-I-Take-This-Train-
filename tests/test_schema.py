from datetime import UTC, datetime

import duckdb
import pytest

from sitt.db import init_db

EXPECTED_TABLES = {"stations", "trains", "scheduled_stops", "observations", "crowd_reports"}


def table_names(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    return {name for (name,) in rows}


def test_init_db_creates_all_tables(tmp_path):
    db_path = tmp_path / "nested" / "test.duckdb"
    with init_db(db_path) as con:
        assert table_names(con) >= EXPECTED_TABLES
    assert db_path.exists()


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "test.duckdb"
    init_db(db_path).close()
    with init_db(db_path) as con:
        assert table_names(con) >= EXPECTED_TABLES


def test_crowd_reports_enforce_constraints(tmp_path):
    now = datetime.now(UTC)
    insert = (
        "INSERT INTO crowd_reports (reported_at, train_description, crowd_level, source) "
        "VALUES (?, ?, ?, 'test')"
    )
    with init_db(tmp_path / "test.duckdb") as con:
        con.execute(insert, [now, "8:12 fast from Kalyan", 4])

        with pytest.raises(duckdb.ConstraintException):
            con.execute(insert, [now, "8:12 fast from Kalyan", 6])
        with pytest.raises(duckdb.ConstraintException):
            con.execute(insert, [now, None, 3])

        assert con.execute("SELECT count(*) FROM crowd_reports").fetchone() == (1,)
