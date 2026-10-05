"""Write raw responses (gzipped JSON) and observation batches (Parquet) to disk.

Layout, relative to the data directory:
    raw/YYYY-MM-DD/<batch_id>-<source>.json.gz
    observations/YYYY-MM-DD/<batch_id>.parquet
Dates are UTC. Each collector run is one batch and writes each file exactly once, so
files never change after they are written. That keeps the git data branch append-only.
"""

import gzip
import json
import secrets
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from sitt.ingest.live.common import Observation, RawResponse

# Parquet columns, in `observations` order (minus the surrogate `id`).
COLUMNS: dict[str, str] = {
    "observed_at": "TIMESTAMPTZ",
    "train_id": "VARCHAR",
    "station_code": "VARCHAR",
    "actual_or_expected_time": "TIMESTAMPTZ",
    "time_kind": "VARCHAR",
    "delay_minutes": "DOUBLE",
    "source": "VARCHAR",
    "train_number": "VARCHAR",
    "event": "VARCHAR",
    "cancelled": "BOOLEAN",
    "less_accurate": "BOOLEAN",
    "raw_status": "VARCHAR",
    "batch_id": "VARCHAR",
}


def new_batch_id(now: datetime | None = None) -> str:
    """Sortable and unique across machines, e.g. '20260927T101500Z-3fa2c1'."""
    now = now or datetime.now(UTC)
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def _day_dir(root: Path, batch_id: str) -> Path:
    stamp = batch_id[:8]  # YYYYMMDD
    return root / f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"


def write_raw(raw: RawResponse, raw_dir: Path, batch_id: str) -> Path:
    path = _day_dir(raw_dir, batch_id) / f"{batch_id}-{raw.source}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = asdict(raw) | {"fetched_at": raw.fetched_at.isoformat(), "batch_id": batch_id}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    return path


def read_raw(path: Path) -> RawResponse:
    """Load an archived response, e.g. to re-run a parser over old data."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        record = json.load(f)
    record.pop("batch_id", None)
    record["fetched_at"] = datetime.fromisoformat(record["fetched_at"])
    record["requests"] = tuple(record["requests"])
    return RawResponse(**record)


def to_row(observation: Observation, batch_id: str) -> dict:
    row = asdict(observation)
    # Unmatched until the loader finds the train in `trains` (see load.rematch).
    row["train_id"] = observation.train_number
    row["batch_id"] = batch_id
    return row


def write_parquet(
    observations: Sequence[Observation], observations_dir: Path, batch_id: str
) -> Path:
    path = _day_dir(observations_dir, batch_id) / f"{batch_id}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [to_row(o, batch_id) for o in observations]
    with duckdb.connect() as con:
        con.execute(
            "CREATE TABLE batch ("
            + ", ".join(f"{name} {kind}" for name, kind in COLUMNS.items())
            + ")"
        )
        con.executemany(
            f"INSERT INTO batch VALUES ({', '.join('?' for _ in COLUMNS)})",
            [[row[name] for name in COLUMNS] for row in rows],
        )
        target = path.as_posix().replace("'", "''")
        con.execute(f"COPY batch TO '{target}' (FORMAT parquet, COMPRESSION zstd)")
    return path
