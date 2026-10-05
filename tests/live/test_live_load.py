from datetime import UTC, datetime

import pytest

from sitt.db import init_db
from sitt.ingest.live.common import Observation
from sitt.ingest.live.load import load
from sitt.ingest.live.storage import new_batch_id, write_parquet

NOW = datetime(2026, 9, 27, 10, 21, tzinfo=UTC)
NEW_COLUMNS = {"train_number", "event", "cancelled", "less_accurate", "raw_status", "batch_id"}


def _obs(number: str, station: str) -> Observation:
    return Observation(NOW, number, station, "at", "mobond", delay_minutes=3.0)


@pytest.fixture
def con(tmp_path):
    with init_db(tmp_path / "test.duckdb") as con:
        con.execute(
            "INSERT INTO trains (train_id, number, label, train_type, line, direction) VALUES "
            "('central-95231', '95231', 'Kalyan', 'fast', 'central', 'down'), "
            "('central-10001', '10001', 'X', 'slow', 'central', 'up'), "
            "('harbour-10001', '10001', 'Y', 'slow', 'harbour', 'up')"
        )
        con.execute(
            "INSERT INTO stations (code, name, line, seq) VALUES "
            "('KDV', 'Khadavli', 'central', 30), ('DR', 'Dadar', 'central', 8)"
        )
        yield con


def test_new_observation_columns_exist_and_schema_reapplies(tmp_path):
    path = tmp_path / "test.duckdb"
    init_db(path).close()
    with init_db(path) as con:  # second run must not fail on the ALTERs
        columns = {row[0] for row in con.execute("DESCRIBE observations").fetchall()}
    assert columns >= NEW_COLUMNS


def test_rematch_trains_and_stations(con, tmp_path):
    observations = [
        _obs("95231", "KHADAVLI"),  # known train, station name only in `stations`
        _obs("69167", "KOPR"),  # train not in the timetable: kept with its raw number
        _obs("10001", "KYN"),  # number on two lines: ambiguous, left raw
        _obs("92126", "DADAR"),  # Western Railway: never mapped to Central's Dadar
    ]
    write_parquet(observations, tmp_path / "obs", new_batch_id(NOW))

    result = load(con, tmp_path / "obs")

    assert (result.rows, result.trains_matched, result.stations_matched) == (4, 1, 1)
    rows = con.execute(
        "SELECT train_number, train_id, station_code FROM observations ORDER BY train_number"
    ).fetchall()
    assert rows == [
        ("10001", "10001", "KYN"),
        ("69167", "69167", "KOPR"),
        ("92126", "92126", "DADAR"),
        ("95231", "central-95231", "KDV"),
    ]


def test_rematch_is_repeatable(con, tmp_path):
    write_parquet([_obs("95231", "KHADAVLI")], tmp_path / "obs", new_batch_id(NOW))
    load(con, tmp_path / "obs")
    again = load(con, tmp_path / "obs")
    assert (again.rows, again.trains_matched, again.stations_matched) == (0, 0, 0)
