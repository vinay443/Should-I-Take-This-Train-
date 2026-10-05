"""The collector's run log (`collector_runs`). No network."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import pytest
from test_live_local import FakeSources

from sitt.db import init_db
from sitt.ingest.live import local, ntes, runlog
from sitt.ingest.live.collect import CollectResult, SourceResult
from sitt.ingest.live.common import Observation, make_client

NOW = datetime(2026, 10, 5, 14, 45, tzinfo=UTC)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(ntes, "PAUSE_SECONDS", 0)
    monkeypatch.delenv("SITT_MOBOND_ENABLED", raising=False)


def _runs(db: Path) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(
            "SELECT source, status, readings, trains_matched, trains_unmatched, error, "
            "host IS NOT NULL, backfilled FROM collector_runs ORDER BY started_at, source"
        ).fetchall()


def _observation(number: str) -> Observation:
    return Observation(NOW, number, "KYN", "arrival", "ntes", delay_minutes=1.0)


def _collected(*results: SourceResult) -> CollectResult:
    return CollectResult("20261005T144500Z-abc123", list(results))


def test_build_records_covers_every_status():
    collected = _collected(SourceResult("ntes", [_observation("95401"), _observation("12127")]))
    stamps = dict(started_at=NOW, finished_at=NOW + timedelta(seconds=4), host="laptop")

    ok = {r.source: r for r in runlog.build_records(collected, **stamps)}
    assert (ok["ntes"].status, ok["ntes"].readings, ok["ntes"].host) == ("ok", 2, "laptop")
    assert ok["mobond"].status == "skipped_disabled" and ok["mobond"].readings == 0

    failed = runlog.build_records(_collected(SourceResult("ntes", error="HTTP 503")), **stamps)
    assert [(r.source, r.status, r.error) for r in failed] == [
        ("mobond", "skipped_disabled", None),
        ("ntes", "failed", "HTTP 503"),
    ]

    empty = runlog.build_records(_collected(SourceResult("ntes")), **stamps)
    assert (empty[1].status, empty[1].error) == ("partial", "no readings parsed")

    unloaded = runlog.build_records(collected, load_error="IOException: file is locked", **stamps)
    assert unloaded[1].status == "partial" and unloaded[1].readings == 2
    assert unloaded[1].error.startswith("not loaded into the database: IOException")


def test_errors_are_short_and_carry_no_query_strings():
    message = "GET https://example.test/api?token=SECRET&x=1 failed:\n  timed out " + "x" * 400
    short = runlog.short_error(message)
    assert "SECRET" not in short and "\n" not in short and len(short) <= runlog.ERROR_LENGTH
    assert short.startswith("GET https://example.test/api failed: timed out")
    assert runlog.short_error(None) is None and runlog.short_error("") is None


def test_every_run_is_recorded_with_match_counts(tmp_path, loaded):
    # Give the timetable one train number the NTES fixture contains, so one train matches.
    db, data_dir = tmp_path / "sitt.duckdb", tmp_path / "data"
    with make_client(httpx.MockTransport(FakeSources())) as client:
        run = local.run_once(data_dir, db, ["ntes"], client)
    assert run.ok
    with duckdb.connect(str(db), read_only=True) as con:
        trains = con.execute("SELECT count(DISTINCT train_number) FROM observations").fetchone()[0]
    assert _runs(db) == [
        ("mobond", "skipped_disabled", 0, None, None, None, True, False),
        ("ntes", "ok", 9, 0, trains, None, True, False),
    ]


def test_a_failed_run_is_recorded_too(tmp_path):
    db = tmp_path / "sitt.duckdb"
    with make_client(httpx.MockTransport(FakeSources(ntes_status=503))) as client:
        run = local.run_once(tmp_path / "data", db, ["ntes"], client)
    assert not run.ok
    ((source, status, readings, _, _, error, _, _),) = [r for r in _runs(db) if r[0] == "ntes"]
    assert (source, status, readings) == ("ntes", "failed", 0)
    assert "HTTP 503" in error


def test_a_busy_database_parks_the_run_log_until_the_next_run(tmp_path, monkeypatch):
    db, data_dir = tmp_path / "sitt.duckdb", tmp_path / "data"

    def busy(path):
        raise duckdb.IOException("File is already open in another process")

    monkeypatch.setattr(local, "init_db", busy)
    with make_client(httpx.MockTransport(FakeSources())) as client:
        blocked = local.run_once(data_dir, db, ["ntes"], client, wait_seconds=0)
    assert not blocked.ok
    pending = runlog.read_pending(data_dir / "logs")
    assert [(r.source, r.status) for r in pending] == [
        ("mobond", "skipped_disabled"),
        ("ntes", "partial"),
    ]

    monkeypatch.setattr(local, "init_db", init_db)
    with make_client(httpx.MockTransport(FakeSources())) as client:
        assert local.run_once(data_dir, db, ["ntes"], client).ok
    assert not (data_dir / "logs" / runlog.PENDING_FILE).exists()
    # The parked run's batch has loaded by now, so its record is promoted to ok.
    assert [(r[0], r[1], r[2]) for r in _runs(db) if r[0] == "ntes"] == [
        ("ntes", "ok", 9),
        ("ntes", "ok", 9),
    ]


def test_a_broken_run_log_never_stops_collection(tmp_path, monkeypatch):
    db, data_dir = tmp_path / "sitt.duckdb", tmp_path / "data"

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runlog, "write_records", broken)
    with make_client(httpx.MockTransport(FakeSources())) as client:
        run = local.run_once(data_dir, db, ["ntes"], client)
    assert run.ok and run.loaded.rows == 9
    assert len(runlog.read_pending(data_dir / "logs")) == 2

    monkeypatch.setattr(runlog, "append_pending", broken)  # even the fallback failing is survivable
    with make_client(httpx.MockTransport(FakeSources())) as client:
        assert local.run_once(data_dir, db, ["ntes"], client).ok


def test_runs_from_before_the_log_existed_are_backfilled(tmp_path):
    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        con.executemany(
            "INSERT INTO observations (observed_at, train_id, station_code, source, "
            "train_number, event, batch_id) VALUES (?, ?, 'KYN', ?, ?, 'arrival', ?)",
            [
                [NOW, "central-95401", "ntes", "95401", "20261005T100000Z-aaaaaa"],
                [NOW, "12127", "ntes", "12127", "20261005T100000Z-aaaaaa"],
                [NOW, "central-95401", "synthetic", "95401", "20260601T000000Z-synth0"],
            ],
        )
        assert runlog.backfill(con) == 1
        assert runlog.backfill(con) == 0  # idempotent
        row = con.execute(
            "SELECT run_id, source, status, readings, trains_matched, trains_unmatched, "
            "backfilled, epoch(started_at) FROM collector_runs"
        ).fetchone()
    assert row[:7] == ("20261005T100000Z-aaaaaa", "ntes", "ok", 2, 1, 1, True)
    assert row[7] == datetime(2026, 10, 5, 10, 0, tzinfo=UTC).timestamp()


def test_pending_file_survives_a_corrupt_line(tmp_path):
    record = runlog.RunRecord("r1", "ntes", NOW, None, "failed", error="x", host="h")
    path = runlog.append_pending(tmp_path, [record])
    path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    assert runlog.read_pending(tmp_path) == [record]
