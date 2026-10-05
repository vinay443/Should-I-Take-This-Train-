"""Parquet export and compaction. Temporary folders only; nothing touches git or the network."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from sitt import export
from sitt.db import DatabaseBusyError, init_db
from sitt.ingest.live.common import Observation
from sitt.ingest.live.load import load
from sitt.ingest.live.storage import COLUMNS, write_parquet
from sitt.tz import IST

SECRET = "Between SHAHAD - AMBIVLI, 21 min Late (a source's own words)"
NOW = datetime(2026, 12, 3, 12, 0, tzinfo=UTC)


def reading(seen: datetime, number: str = "95401", delay: float = 3.0) -> list:
    """[observed_at, train_id, station, source, number, event, delay, raw_status, batch_id]."""
    batch = f"{seen.astimezone(UTC):%Y%m%dT%H%M%SZ}-aaaaaa"
    return [seen, f"central-{number}", "KYN", "ntes", number, "arrival", delay, SECRET, batch]


def insert(con, rows: list[list]) -> None:
    con.executemany(
        "INSERT INTO observations (observed_at, train_id, station_code, source, train_number, "
        "event, delay_minutes, raw_status, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        insert(
            con,
            [
                reading(datetime(2026, 10, 5, 20, 0, tzinfo=IST)),
                reading(datetime(2026, 10, 5, 20, 15, tzinfo=IST), "95403"),
                reading(datetime(2026, 10, 31, 23, 0, tzinfo=IST)),
                # 02:00 IST on 1 November is still October in UTC.
                reading(datetime(2026, 11, 1, 2, 0, tzinfo=IST)),
                reading(datetime(2026, 11, 1, 9, 0, tzinfo=IST)),
            ],
        )
        insert(con, [reading(datetime(2026, 10, 5, 20, 0, tzinfo=IST))])  # an exact duplicate
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, source, "
            "train_number, event, batch_id) VALUES (?, 'central-95401', 'KYN', 'synthetic', "
            "'95401', 'at', '20261005T000000Z-synth0')",
            [datetime(2026, 10, 5, 12, 0, tzinfo=IST)],
        )
    return path


def rows_of(path: Path) -> list[tuple]:
    with duckdb.connect() as con:
        return con.execute(
            f"SELECT train_number, epoch(observed_at), delay_minutes FROM '{path.as_posix()}'"
        ).fetchall()


def everything_in(path: Path) -> str:
    """Every value in a Parquet file, as text, to search for something that must not be there."""
    with duckdb.connect() as con:
        query = f"SELECT CAST(COLUMNS(*) AS VARCHAR) FROM '{path.as_posix()}'"
        return str(con.execute(query).fetchall())


# --- raw_status is never exported ---


def test_raw_status_is_not_an_export_column():
    assert "raw_status" not in COLUMNS
    assert {"raw_status"} == export.FORBIDDEN_COLUMNS


def test_exported_files_never_contain_raw_status(db, tmp_path):
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(raw_status) FROM observations").fetchone() == (6,)
        results = export.export(con, tmp_path / "out")
    assert results
    for result in results:
        assert export.parquet_columns(result.path) == list(COLUMNS)
        assert "raw_status" not in export.parquet_columns(result.path)
        assert SECRET not in everything_in(result.path) and "SHAHAD" not in everything_in(
            result.path
        )
        assert SECRET.encode() not in result.path.read_bytes()


def test_compaction_drops_raw_status_even_if_an_input_file_has_it(tmp_path):
    folder = tmp_path / "observations" / "2026-10-05"
    folder.mkdir(parents=True)
    leaky = folder / "20261005T143000Z-leaky0.parquet"
    with duckdb.connect() as con:
        con.execute(
            "CREATE TABLE t AS SELECT TIMESTAMPTZ '2026-10-05 14:30:00+00' AS observed_at, "
            "'95401' AS train_id, 'KYN' AS station_code, NULL::TIMESTAMPTZ AS "
            "actual_or_expected_time, NULL::VARCHAR AS time_kind, 3.0 AS delay_minutes, "
            "'mobond' AS source, '95401' AS train_number, 'at' AS event, false AS cancelled, "
            "false AS less_accurate, '20261005T143000Z-leaky0' AS batch_id, ? AS raw_status",
            [SECRET],
        )
        con.execute(f"COPY t TO '{leaky.as_posix()}' (FORMAT parquet)")
    assert "raw_status" in export.parquet_columns(leaky)

    (result,) = export.compact(tmp_path / "observations", now=NOW)
    assert export.parquet_columns(result.path) == list(COLUMNS)
    assert SECRET.encode() not in result.path.read_bytes()
    assert rows_of(result.path) == [("95401", 1791210600.0, 3.0)]


# --- export ---


def test_export_writes_one_file_per_utc_month_without_duplicates_or_synthetic_rows(db, tmp_path):
    with duckdb.connect(str(db), read_only=True) as con:
        results = export.export(con, tmp_path / "out")
    assert [(r.month, r.rows, r.written) for r in results] == [
        ("2026-10", 4, True),
        ("2026-11", 1, True),
    ]
    assert results[0].path == tmp_path / "out" / "observations" / "2026-10.parquet"
    assert results[0].duplicates == 1
    october = rows_of(results[0].path)
    assert len(october) == 4 and len(set(october)) == 4
    with duckdb.connect() as con:
        sources = con.execute(
            f"SELECT DISTINCT source FROM '{results[0].path.as_posix()}'"
        ).fetchall()
    assert sources == [("ntes",)]
    # Rows are in a fixed order.
    assert [row[1] for row in october] == sorted(row[1] for row in october)


def test_export_is_idempotent_and_picks_up_new_rows(db, tmp_path):
    out = tmp_path / "out"
    with duckdb.connect(str(db), read_only=True) as con:
        export.export(con, out)
        before = {p.name: rows_of(p) for p in sorted((out / "observations").iterdir())}
        again = export.export(con, out)
    assert [r.written for r in again] == [False, False]
    assert {p.name: rows_of(p) for p in sorted((out / "observations").iterdir())} == before
    assert not list(out.rglob("*.tmp"))

    with duckdb.connect(str(db)) as con:
        insert(con, [reading(datetime(2026, 11, 2, 9, 0, tzinfo=IST))])
        later = export.export(con, out)
    assert [(r.month, r.rows, r.written) for r in later] == [
        ("2026-10", 4, False),
        ("2026-11", 2, True),
    ]


def test_cli_export(db, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert export.main(["export", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "2026-10.parquet: 4 rows, written" in out
    assert "1 exact duplicate row(s) were left out." in out
    assert "raw_status is not in these files." in out
    assert (tmp_path / "scratch" / "export" / "observations" / "2026-11.parquet").is_file()

    assert export.main(["export", "--db", str(tmp_path / "nope.duckdb")]) == 2
    assert "No database at" in capsys.readouterr().err

    def busy(*args, **kwargs):
        raise DatabaseBusyError("sitt.duckdb is in use by another process")

    monkeypatch.setattr(export, "open_with_retry", busy)
    assert export.main(["export", "--db", str(db)]) == 2
    assert "in use by another process" in capsys.readouterr().err


# --- compaction ---


def collector_files(root: Path, start: datetime, runs: int, minutes: int = 15) -> list[Path]:
    """Per-run files as the collector writes them: two readings a run."""
    paths = []
    for index in range(runs):
        seen = start + timedelta(minutes=minutes * index)
        batch = f"{seen:%Y%m%dT%H%M%SZ}-{index:06x}"
        observations = [
            Observation(seen, number, "KYN", "arrival", "ntes", delay_minutes=float(index % 5),
                        raw_status=SECRET)
            for number in ("95401", "12127")
        ]  # fmt: skip
        paths.append(write_parquet(observations, root, batch))
    return paths


def test_compaction_merges_finished_months_and_leaves_the_current_one(tmp_path):
    root = tmp_path / "observations"
    october = collector_files(root, datetime(2026, 10, 30, 22, 0, tzinfo=UTC), runs=6)
    november = collector_files(root, datetime(2026, 11, 30, 23, 30, tzinfo=UTC), runs=4)
    december = collector_files(root, datetime(2026, 12, 2, 10, 0, tzinfo=UTC), runs=3)

    results = export.compact(root, now=NOW)
    assert [(r.month, r.rows, len(r.merged), r.written) for r in results] == [
        ("2026-10", 12, 6, True),
        ("2026-11", 4, 2, True),  # two of the four runs fall after midnight UTC, in December
    ]
    assert len(rows_of(root / "2026-10.parquet")) == 12
    assert not (root / "2026-12.parquet").exists()  # the collector is still writing December
    # Nothing is removed unless asked for.
    assert all(path.is_file() for path in october + november + december)


def test_compaction_dedupes_is_idempotent_and_deletes_only_when_told(tmp_path):
    root = tmp_path / "observations"
    files = collector_files(root, datetime(2026, 10, 5, 14, 0, tzinfo=UTC), runs=5)
    # The same batch saved twice under another name: every row a duplicate.
    copy = files[0].with_name("20261005T140000Z-copied.parquet")
    copy.write_bytes(files[0].read_bytes())

    (first,) = export.compact(root, now=NOW)
    assert (first.rows, first.duplicates, first.written) == (10, 2, True)
    content = rows_of(first.path)

    (second,) = export.compact(root, now=NOW)  # monthly file plus the small ones again
    assert (second.rows, second.written) == (10, False)
    assert rows_of(first.path) == content

    (third,) = export.compact(root, now=NOW, delete_merged=True)
    assert (third.rows, third.written, len(third.merged)) == (10, False, 6)
    assert sorted(p.name for p in root.rglob("*.parquet")) == ["2026-10.parquet"]
    assert not (root / "2026-10-05").exists()  # the emptied day folder goes too

    (fourth,) = export.compact(root, now=NOW, delete_merged=True)  # only the monthly file left
    assert (fourth.rows, fourth.written, fourth.merged) == (10, False, [])
    assert rows_of(first.path) == content


def test_late_files_are_folded_into_an_existing_monthly_file(tmp_path):
    root = tmp_path / "observations"
    collector_files(root, datetime(2026, 10, 5, 14, 0, tzinfo=UTC), runs=3)
    export.compact(root, now=NOW, delete_merged=True)
    collector_files(root, datetime(2026, 10, 20, 9, 0, tzinfo=UTC), runs=2)
    (result,) = export.compact(root, now=NOW, delete_merged=True)
    assert (result.rows, result.written, len(result.merged)) == (10, True, 2)
    assert sorted(p.name for p in root.rglob("*.parquet")) == ["2026-10.parquet"]


def test_the_loader_reads_compacted_files_without_duplicating_rows(tmp_path):
    root = tmp_path / "observations"
    collector_files(root, datetime(2026, 10, 5, 14, 0, tzinfo=UTC), runs=4)
    with init_db(tmp_path / "before.duckdb") as con:
        assert load(con, root).rows == 8  # loaded from the per-run files
        export.compact(root, now=NOW)  # now both the small files and the monthly one exist
        assert load(con, root).rows == 0
        assert con.execute("SELECT count(*) FROM observations").fetchone() == (8,)
    export.compact(root, now=NOW, delete_merged=True)
    with init_db(tmp_path / "after.duckdb") as con:
        assert load(con, root).rows == 8  # a fresh database, from the monthly file alone
        assert con.execute("SELECT count(DISTINCT batch_id) FROM observations").fetchone() == (4,)


def test_cli_compact(tmp_path, capsys):
    root = tmp_path / "observations"
    assert export.main(["compact", str(root)]) == 2
    assert "No such folder" in capsys.readouterr().err
    root.mkdir()
    assert export.main(["compact", str(root)], now=NOW) == 0
    assert "Nothing to compact" in capsys.readouterr().out

    collector_files(root, datetime(2026, 10, 5, 14, 0, tzinfo=UTC), runs=3)
    assert export.main(["compact", str(root)], now=NOW) == 0
    out = capsys.readouterr().out
    assert "2026-10.parquet: 6 rows from 3 small file(s), written" in out
    assert "3 small file(s) kept. Run again with --delete-merged to remove them." in out
    assert export.main(["compact", str(root), "--delete-merged"], now=NOW) == 0
    assert "Removed 3 small file(s)." in capsys.readouterr().out
    # The current month only when asked.
    collector_files(root, datetime(2026, 12, 2, 10, 0, tzinfo=UTC), runs=2)
    assert export.main(["compact", str(root), "--include-current"], now=NOW) == 0
    assert (root / "2026-12.parquet").is_file()
