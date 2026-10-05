"""The local collector (`sitt-collect`): one run, appended to a database. No network."""

from pathlib import Path
from urllib.parse import parse_qs

import duckdb
import httpx
import pytest

from sitt.db import init_db
from sitt.ingest.live import local, mobond, ntes
from sitt.ingest.live.common import make_client
from sitt.ingest.live.local import enabled_sources, load_with_retry, run_once

FIXTURES = Path(__file__).parents[1] / "fixtures" / "live"
MOBOND_BODY = (FIXTURES / "mobond_getalllivetrains.json").read_text(encoding="utf-8")
NTES_BOARD = (FIXTURES / "ntes_live_station_kyn.html").read_text(encoding="utf-8")


class FakeSources:
    def __init__(self, ntes_status: int = 200):
        self.hosts: list[str] = []
        self.ntes_status = ntes_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        url = str(request.url)
        if url == mobond.URL:
            return httpx.Response(200, text=MOBOND_BODY)
        if url == ntes.BASE_URL:
            return httpx.Response(self.ntes_status, text="<html>home</html>")
        if url.startswith(f"{ntes.BASE_URL}GetCSRFToken"):
            return httpx.Response(200, text="<input type='hidden' name='tk1' value='v1'>")
        if url.startswith(f"{ntes.BASE_URL}q?"):
            assert parse_qs(request.content.decode())["jFromStationInput"] == ["KYN"]
            return httpx.Response(200, text=NTES_BOARD)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(ntes, "PAUSE_SECONDS", 0)
    monkeypatch.delenv("SITT_MOBOND_ENABLED", raising=False)


def _rows(db: Path) -> dict[str, int]:
    with duckdb.connect(str(db), read_only=True) as con:
        return dict(con.execute("SELECT source, count(*) FROM observations GROUP BY 1").fetchall())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ["ntes"]),
        ("", ["ntes"]),
        ("false", ["ntes"]),
        ("1", ["ntes"]),  # only the word "true" switches it on, as in collect.yml
        ("yes", ["ntes"]),
        ("true", ["mobond", "ntes"]),
        (" TRUE ", ["mobond", "ntes"]),
    ],
)
def test_mobond_is_off_unless_switched_on(value, expected):
    environ = {} if value is None else {"SITT_MOBOND_ENABLED": value}
    assert enabled_sources(environ) == expected


def test_one_run_appends_to_the_database(tmp_path):
    db, data_dir = tmp_path / "sitt.duckdb", tmp_path / "data"
    fake = FakeSources()
    with make_client(httpx.MockTransport(fake)) as client:
        first = run_once(data_dir, db, enabled_sources({}), client)
        second = run_once(data_dir, db, enabled_sources({}), client)

    assert first.ok and second.ok
    assert "mobond.com" not in fake.hosts  # off by default: not a single request
    assert fake.hosts.count("enquiry.indianrail.gov.in") == 6  # 3 per run
    assert (first.loaded.files, first.loaded.rows) == (1, 9)
    assert (second.loaded.files, second.loaded.rows) == (1, 9)  # only the new batch
    assert _rows(db) == {"ntes": 18}
    assert len(list((data_dir / "observations").rglob("*.parquet"))) == 2
    assert len(list((data_dir / "raw").rglob("*-ntes.json.gz"))) == 2


def test_mobond_is_fetched_once_when_enabled(tmp_path):
    db = tmp_path / "sitt.duckdb"
    fake = FakeSources()
    with make_client(httpx.MockTransport(fake)) as client:
        run = run_once(
            tmp_path / "data", db, enabled_sources({"SITT_MOBOND_ENABLED": "true"}), client
        )
    assert run.ok and fake.hosts.count("mobond.com") == 1
    assert _rows(db) == {"mobond": 40, "ntes": 9}
    with duckdb.connect(str(db), read_only=True) as con:
        # The source's own text stays out of the database, as it does on the data branch.
        assert con.execute("SELECT count(raw_status) FROM observations").fetchone() == (0,)


def test_a_failed_source_still_loads_the_rest_and_reports_failure(tmp_path):
    db = tmp_path / "sitt.duckdb"
    fake = FakeSources(ntes_status=503)
    with make_client(httpx.MockTransport(fake)) as client:
        run = run_once(tmp_path / "data", db, ["mobond", "ntes"], client)
    assert not run.ok and run.load_error is None
    assert _rows(db) == {"mobond": 40}

    # Nothing collected at all: no load is attempted, and the database isn't created.
    with make_client(httpx.MockTransport(FakeSources(ntes_status=503))) as client:
        nothing = run_once(tmp_path / "empty", tmp_path / "none.duckdb", ["ntes"], client)
    assert not nothing.ok and nothing.loaded is None
    assert not (tmp_path / "none.duckdb").exists()


def test_a_busy_database_keeps_the_batch_for_the_next_run(tmp_path, monkeypatch):
    db, data_dir = tmp_path / "sitt.duckdb", tmp_path / "data"
    attempts = []

    def busy(path):
        attempts.append(path)
        raise duckdb.IOException("File is already open in another process")

    monkeypatch.setattr(local, "init_db", busy)
    with make_client(httpx.MockTransport(FakeSources())) as client:
        blocked = run_once(data_dir, db, ["ntes"], client, wait_seconds=0)
    assert not blocked.ok and "already open" in blocked.load_error
    assert len(attempts) == local.LOAD_ATTEMPTS
    assert len(list((data_dir / "observations").rglob("*.parquet"))) == 1  # not lost

    monkeypatch.setattr(local, "init_db", init_db)
    with make_client(httpx.MockTransport(FakeSources())) as client:
        later = run_once(data_dir, db, ["ntes"], client)
    assert later.ok and later.loaded.files == 2  # this run's batch and the one left behind
    assert _rows(db) == {"ntes": 18}


def test_load_retries_then_succeeds(tmp_path, monkeypatch):
    calls = []

    def flaky(path):
        calls.append(path)
        if len(calls) < 3:
            raise duckdb.IOException("busy")
        return init_db(path)

    monkeypatch.setattr(local, "init_db", flaky)
    (tmp_path / "observations").mkdir()
    result = load_with_retry(tmp_path / "sitt.duckdb", tmp_path / "observations", wait_seconds=0)
    assert len(calls) == 3 and result.rows == 0


def test_cli(tmp_path, monkeypatch, capsys):
    transport = httpx.MockTransport(FakeSources())
    monkeypatch.setattr(local, "make_client", lambda: make_client(transport))
    monkeypatch.chdir(tmp_path)  # no .env here
    db, data_dir = tmp_path / "sitt.duckdb", tmp_path / "data"

    assert local.main(["--data-dir", str(data_dir), "--db", str(db)]) == 0
    log = (data_dir / "logs" / "collector.log").read_text(encoding="utf-8")
    assert "Mobond is off (set SITT_MOBOND_ENABLED=true to enable it)" in log
    assert "ntes: 9 observations" in log and "loaded 9 rows from 1 new batch(es)" in log
    assert _rows(db) == {"ntes": 9}

    monkeypatch.setenv("SITT_MOBOND_ENABLED", "true")
    assert local.main(["--data-dir", str(data_dir), "--db", str(db), "--no-log-file"]) == 0
    assert _rows(db) == {"mobond": 40, "ntes": 18}
