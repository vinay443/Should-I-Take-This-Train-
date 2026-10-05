"""End-to-end collector runs against a fake HTTP transport. No network."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

import sitt.ingest.live.__main__ as cli
from sitt.db import init_db
from sitt.ingest.live import mobond, ntes
from sitt.ingest.live.collect import collect
from sitt.ingest.live.common import make_client, user_agent
from sitt.ingest.live.load import load
from sitt.ingest.live.storage import read_raw

FIXTURES = Path(__file__).parents[1] / "fixtures" / "live"
MOBOND_BODY = (FIXTURES / "mobond_getalllivetrains.json").read_text(encoding="utf-8")
NTES_BOARD = (FIXTURES / "ntes_live_station_kyn.html").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 27, 10, 21, tzinfo=UTC)  # 15:51 IST, when the NTES fixture was saved


class FakeSources:
    """Serves the fixtures and records every request made."""

    def __init__(self, mobond_status: int = 200, ntes_board: str = NTES_BOARD):
        self.mobond_status = mobond_status
        self.ntes_board = ntes_board
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == mobond.URL:
            return httpx.Response(self.mobond_status, text=MOBOND_BODY)
        if url == ntes.BASE_URL:
            return httpx.Response(200, text="<html>home</html>", headers={"set-cookie": "s=1"})
        if url.startswith(f"{ntes.BASE_URL}GetCSRFToken"):
            return httpx.Response(200, text="<input type='hidden' name='tk1' value='v1'>")
        if url.startswith(f"{ntes.BASE_URL}q?"):
            form = parse_qs(request.content.decode())
            assert form["jFromStationInput"] == ["KYN"] and form["tk1"] == ["v1"]
            assert request.headers.get("cookie") == "s=1"
            return httpx.Response(200, text=self.ntes_board)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(ntes, "PAUSE_SECONDS", 0)


def _run(tmp_path: Path, fake: FakeSources, sources=("mobond", "ntes"), dry_run=False):
    with make_client(httpx.MockTransport(fake)) as client:
        return collect(
            sources,
            client,
            observations_dir=tmp_path / "observations",
            raw_dir=tmp_path / "raw",
            dry_run=dry_run,
            now=NOW,
        )


def test_collects_both_sources_politely(tmp_path):
    fake = FakeSources()
    result = _run(tmp_path, fake)

    assert result.ok
    assert [len(s.observations) for s in result.sources] == [40, 9]
    # One request for Mobond; NTES needs cookie + token + board.
    hosts = [r.url.host for r in fake.requests]
    assert hosts.count("mobond.com") == 1
    assert hosts.count("enquiry.indianrail.gov.in") == 3
    assert all(r.headers["user-agent"] == user_agent() for r in fake.requests)

    # Raw responses are archived per source and round-trip.
    for source in result.sources:
        assert source.raw_path.parent.name == "2026-09-27"
        assert source.raw_path.name.endswith(f"-{source.source}.json.gz")
        assert read_raw(source.raw_path).body in (MOBOND_BODY, NTES_BOARD)

    assert result.parquet_path == (
        tmp_path / "observations" / "2026-09-27" / f"{result.batch_id}.parquet"
    )


def test_parquet_loads_into_duckdb_once(tmp_path):
    _run(tmp_path, FakeSources())
    with init_db(tmp_path / "test.duckdb") as con:
        first = load(con, tmp_path / "observations")
        again = load(con, tmp_path / "observations")
        rows = con.execute(
            "SELECT source, count(*), count(*) FILTER (cancelled), "
            "count(*) FILTER (train_id = train_number) "
            "FROM observations GROUP BY source ORDER BY source"
        ).fetchall()
    assert (first.files, first.rows) == (1, 49)
    assert (again.files, again.rows) == (0, 0)
    # No timetable loaded, so every train keeps its raw number as train_id.
    assert rows == [("mobond", 40, 1, 40), ("ntes", 9, 0, 9)]


def test_one_failing_source_does_not_stop_the_other(tmp_path):
    result = _run(tmp_path, FakeSources(mobond_status=503))

    mobond_result, ntes_result = result.sources
    assert not result.ok
    assert "HTTP 503" in mobond_result.error
    assert mobond_result.raw_path is None
    assert ntes_result.ok and len(ntes_result.observations) == 9
    assert result.parquet_path.exists()


def test_unparseable_response_is_still_archived(tmp_path):
    result = _run(tmp_path, FakeSources(ntes_board="<html>Please try later</html>"))

    mobond_result, ntes_result = result.sources
    assert mobond_result.ok
    assert "no Live Station table" in ntes_result.error
    assert read_raw(ntes_result.raw_path).body == "<html>Please try later</html>"


def test_dry_run_writes_nothing(tmp_path):
    result = _run(tmp_path, FakeSources(), dry_run=True)
    assert result.ok and len(result.observations) == 49
    assert list(tmp_path.iterdir()) == []


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_client", lambda: make_client(httpx.MockTransport(FakeSources())))
    assert cli.main(["--source", "mobond", "--data-dir", str(tmp_path)]) == 0
    assert "mobond: 40 observations" in capsys.readouterr().out
    assert len(list((tmp_path / "observations").rglob("*.parquet"))) == 1


def test_user_agent(monkeypatch):
    monkeypatch.delenv("SITT_USER_AGENT", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/should_i_take_this_train")
    assert user_agent().endswith("+https://github.com/someone/should_i_take_this_train)")
    monkeypatch.setenv("SITT_USER_AGENT", "custom/1.0")
    assert user_agent() == "custom/1.0"
