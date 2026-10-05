"""Mobond's double opt-in, rate limit and backoff. A fake transport only: no real requests.

The feed served here is the hand-written fixture (see tests/fixtures/live/README.md).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import pytest
from test_live_local import FakeSources

from sitt.ingest.live import local, mobond, ntes, probe
from sitt.ingest.live.collect import collect
from sitt.ingest.live.common import SourceError, make_client

BOTH = {mobond.ENABLE_VARIABLE: "true", mobond.PERMISSION_VARIABLE: "true"}
T0 = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "live" / "mobond_getalllivetrains.json"


class Feed:
    """Counts requests and answers with the fixture, or with a chosen status."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert str(request.url) == mobond.URL
        return httpx.Response(self.status, text=FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (mobond.ENABLE_VARIABLE, mobond.PERMISSION_VARIABLE,
                 mobond.MIN_INTERVAL_VARIABLE, "SITT_USER_AGENT"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ntes, "PAUSE_SECONDS", 0)


def attempt(feed: Feed, state: Path, now: datetime, environ=BOTH):
    with make_client(httpx.MockTransport(feed)) as client:
        return mobond.fetch(client, state_file=state, environ=environ, now=now)


# --- the double opt-in ---


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {mobond.ENABLE_VARIABLE: "true"},
        {mobond.PERMISSION_VARIABLE: "true"},
        {mobond.ENABLE_VARIABLE: "true", mobond.PERMISSION_VARIABLE: "false"},
        {mobond.ENABLE_VARIABLE: "yes", mobond.PERMISSION_VARIABLE: "yes"},
        {mobond.ENABLE_VARIABLE: "1", mobond.PERMISSION_VARIABLE: "true"},
    ],
)
def test_no_request_is_made_without_both_switches(tmp_path, environ):
    feed = Feed()
    with pytest.raises(mobond.MobondDisabledError, match="Mobond is off"):
        attempt(feed, tmp_path / "state.json", T0, environ)
    assert feed.requests == []
    assert not (tmp_path / "state.json").exists()  # nothing happened, so nothing is recorded
    assert mobond.opt_in(environ)[0] is False


def test_the_reason_names_the_missing_switch():
    assert mobond.opt_in(BOTH) == (True, "")
    only_enabled = mobond.opt_in({mobond.ENABLE_VARIABLE: "true"})[1]
    assert "SITT_MOBOND_PERMISSION_CONFIRMED is not" in only_enabled
    assert "only once Mobond has agreed" in only_enabled
    only_permitted = mobond.opt_in({mobond.PERMISSION_VARIABLE: "true"})[1]
    assert "SITT_MOBOND_ENABLED is not" in only_permitted
    assert "needs both" in mobond.opt_in({})[1]


def test_the_environment_is_what_decides_when_none_is_passed(tmp_path, monkeypatch):
    feed = Feed()
    with make_client(httpx.MockTransport(feed)) as client:
        with pytest.raises(mobond.MobondDisabledError):
            mobond.fetch(client, state_file=tmp_path / "s.json", now=T0)
        monkeypatch.setenv(mobond.ENABLE_VARIABLE, "true")
        with pytest.raises(mobond.MobondDisabledError):
            mobond.fetch(client, state_file=tmp_path / "s.json", now=T0)
        monkeypatch.setenv(mobond.PERMISSION_VARIABLE, "true")
        raw = mobond.fetch(client, state_file=tmp_path / "s.json", now=T0)
    assert len(feed.requests) == 1 and raw.status_code == 200


def test_the_collector_and_probe_cannot_bypass_the_gate(tmp_path, capsys, monkeypatch):
    """Naming Mobond explicitly, with the switches off, still makes no request to it."""
    fake = FakeSources()
    with make_client(httpx.MockTransport(fake)) as client:
        result = collect(
            ["mobond", "ntes"],
            client,
            observations_dir=tmp_path / "observations",
            raw_dir=tmp_path / "raw",
            state_dir=tmp_path / "logs",
        )
        run = local.run_once(
            tmp_path / "data", tmp_path / "sitt.duckdb", ["mobond", "ntes"], client
        )
    assert "mobond.com" not in fake.hosts
    assert result.ok and result.sources[0].skipped.startswith("Mobond is off")
    assert result.sources[0].observations == [] and len(result.sources[1].observations) == 9
    assert run.ok
    with duckdb.connect(str(tmp_path / "sitt.duckdb"), read_only=True) as con:
        assert con.execute(
            "SELECT status, readings FROM collector_runs WHERE source = 'mobond'"
        ).fetchall() == [("skipped_disabled", 0)]

    monkeypatch.setattr(probe, "make_client", lambda: make_client(httpx.MockTransport(fake)))
    assert probe.main(["mobond"]) == 0
    assert "NOT FETCHED: Mobond is off" in capsys.readouterr().out
    assert "mobond.com" not in fake.hosts


# --- rate limit ---


def test_at_most_one_request_per_interval(tmp_path):
    feed, state = Feed(), tmp_path / "logs" / "mobond_state.json"
    raw = attempt(feed, state, T0)
    assert raw.fetched_at == T0 and len(mobond.parse_response(raw)) == 40
    assert json.loads(state.read_text("utf-8")) == {
        "last_request_at": T0.isoformat(),
        "failures": 0,
    }

    for minutes in (1, 5, 13):
        with pytest.raises(
            mobond.MobondSkipped, match=r"rate limit\): next request allowed at 03:15"
        ):
            attempt(feed, state, T0 + timedelta(minutes=minutes))
    assert len(feed.requests) == 1

    # The scheduled run a few seconds short of the quarter hour is let through.
    attempt(feed, state, T0 + timedelta(minutes=14, seconds=30))
    assert len(feed.requests) == 2


def test_the_interval_can_be_lengthened_but_never_shortened():
    assert mobond.min_interval({}) == timedelta(minutes=15)
    assert mobond.min_interval({mobond.MIN_INTERVAL_VARIABLE: "30"}) == timedelta(minutes=30)
    assert mobond.min_interval({mobond.MIN_INTERVAL_VARIABLE: "1"}) == timedelta(minutes=15)
    assert mobond.min_interval({mobond.MIN_INTERVAL_VARIABLE: "0"}) == timedelta(minutes=15)
    assert mobond.min_interval({mobond.MIN_INTERVAL_VARIABLE: "soon"}) == timedelta(minutes=15)


def test_a_longer_interval_is_honoured(tmp_path):
    feed, state = Feed(), tmp_path / "state.json"
    slow = BOTH | {mobond.MIN_INTERVAL_VARIABLE: "30"}
    attempt(feed, state, T0, slow)
    with pytest.raises(mobond.MobondSkipped):
        attempt(feed, state, T0 + timedelta(minutes=20), slow)
    attempt(feed, state, T0 + timedelta(minutes=30), slow)
    assert len(feed.requests) == 2


# --- backoff ---


def test_failures_double_the_wait_and_a_success_resets_it(tmp_path):
    state = tmp_path / "state.json"
    down = Feed(status=503)
    with pytest.raises(SourceError, match="HTTP 503"):
        attempt(down, state, T0)
    assert json.loads(state.read_text("utf-8"))["failures"] == 1

    # After one failure the wait is 30 minutes, not 15.
    with pytest.raises(mobond.MobondSkipped, match="backing off after 1 failed request"):
        attempt(down, state, T0 + timedelta(minutes=15))
    assert len(down.requests) == 1
    second = T0 + timedelta(minutes=30)
    with pytest.raises(SourceError):
        attempt(down, state, second)
    # After two, an hour.
    with pytest.raises(mobond.MobondSkipped, match="backing off after 2 failed request"):
        attempt(down, state, second + timedelta(minutes=45))
    assert len(down.requests) == 2

    up = Feed()
    attempt(up, state, second + timedelta(minutes=60))
    assert json.loads(state.read_text("utf-8"))["failures"] == 0
    attempt(up, state, second + timedelta(minutes=75))  # back to the ordinary interval
    assert len(up.requests) == 2


def test_backoff_is_capped_at_a_day():
    quarter = timedelta(minutes=15)
    assert mobond.wait_after(0, quarter) == quarter
    assert mobond.wait_after(3, quarter) == timedelta(hours=2)
    assert mobond.wait_after(10, quarter) == mobond.MAX_BACKOFF == timedelta(hours=24)
    assert mobond.wait_after(500, quarter) == mobond.MAX_BACKOFF


def test_a_network_error_counts_as_a_failure(tmp_path):
    def refuse(request):
        raise httpx.ConnectTimeout("timed out")

    state = tmp_path / "state.json"
    with make_client(httpx.MockTransport(refuse)) as client, pytest.raises(SourceError):
        mobond.fetch(client, state_file=state, environ=BOTH, now=T0)
    assert json.loads(state.read_text("utf-8"))["failures"] == 1


def test_an_unreadable_state_file_is_treated_as_a_request_just_made(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json", "utf-8")
    feed = Feed()
    with pytest.raises(mobond.MobondSkipped, match="could not be read, so it was reset"):
        attempt(feed, state, T0)
    assert feed.requests == []
    with pytest.raises(mobond.MobondSkipped, match="rate limit"):
        attempt(feed, state, T0 + timedelta(minutes=5))
    attempt(feed, state, T0 + timedelta(minutes=15))
    assert len(feed.requests) == 1


def test_no_request_if_the_request_time_cannot_be_recorded(tmp_path, monkeypatch):
    def read_only(*args, **kwargs):
        raise OSError("disk is read-only")

    monkeypatch.setattr(mobond, "save_state", read_only)
    feed = Feed()
    with pytest.raises(mobond.MobondSkipped, match="can't be recorded"):
        attempt(feed, tmp_path / "state.json", T0)
    assert feed.requests == []


# --- manners ---


def test_the_request_identifies_itself_and_has_short_timeouts(tmp_path):
    feed = Feed()
    attempt(feed, tmp_path / "state.json", T0)
    (request,) = feed.requests
    agent = request.headers["user-agent"]
    assert agent == mobond.mobond_user_agent(BOTH)
    assert "should-i-take-this-train" in agent and "personal non-commercial research" in agent
    assert "at most one request every 15 minutes" in agent
    assert "https://github.com/vinay443/Should-I-Take-This-Train-" in agent
    assert request.headers["accept"] == "application/json" and request.method == "GET"
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    slow = mobond.mobond_user_agent(BOTH | {mobond.MIN_INTERVAL_VARIABLE: "30"})
    assert "every 30 minutes" in slow
    assert mobond.mobond_user_agent({"SITT_USER_AGENT": "mine/1.0"}) == "mine/1.0"


def test_a_rate_limited_run_is_recorded_as_not_polled_rather_than_failed(tmp_path, monkeypatch):
    for name, value in BOTH.items():
        monkeypatch.setenv(name, value)
    db, data_dir = tmp_path / "sitt.duckdb", tmp_path / "data"
    fake = FakeSources()
    with make_client(httpx.MockTransport(fake)) as client:
        first = local.run_once(data_dir, db, local.enabled_sources(), client)
        second = local.run_once(data_dir, db, local.enabled_sources(), client)  # seconds later
    assert first.ok and second.ok
    assert fake.hosts.count("mobond.com") == 1
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute(
            "SELECT status, readings, error FROM collector_runs WHERE source = 'mobond' "
            "ORDER BY started_at"
        ).fetchall()
    assert rows[0] == ("ok", 40, None)
    assert rows[1][:2] == ("skipped_disabled", 0) and "rate limit" in rows[1][2]


# --- the fixture is labelled for what it is ---


def test_the_fixture_is_documented_as_hand_written():
    readme = (FIXTURE.parent / "README.md").read_text(encoding="utf-8")
    assert "hand-written" in readme and "may be inaccurate" in readme
    assert "mobond_getalllivetrains.json" in readme
