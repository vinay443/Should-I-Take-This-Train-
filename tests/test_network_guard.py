"""The safety rails: tests can't reach the network, and Mobond stays off by default."""

import socket

import httpx
import pytest

from sitt.ingest.live import local, mobond, ntes
from sitt.ingest.live.collect import SOURCES
from sitt.ingest.live.common import make_client

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy")


@pytest.fixture
def default_environment(tmp_path, monkeypatch):
    """No .env, no Mobond switch, no proxy: the configuration a fresh checkout has."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(local.MOBOND_SWITCH, raising=False)
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ntes, "PAUSE_SECONDS", 0)
    return tmp_path


@pytest.mark.expects_blocked_network
def test_a_real_connection_is_refused(blocked_network):
    with pytest.raises(OSError, match="tests must not use the network"):
        socket.create_connection(("example.com", 443), timeout=1)
    with pytest.raises(httpx.ConnectError), httpx.Client() as client:
        client.get("https://example.org/")
    assert blocked_network == ["example.com", "example.org"]


def test_this_machine_is_still_reachable():
    # asyncio's event loop on Windows, and Streamlit's test harness, use loopback sockets.
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2):
            pass
    finally:
        server.close()


def test_mobond_is_disabled_by_default(default_environment):
    assert local.enabled_sources() == [ntes.SOURCE]
    assert mobond.SOURCE not in local.enabled_sources({})


@pytest.mark.expects_blocked_network
def test_the_default_collector_never_calls_mobond(
    default_environment, blocked_network, monkeypatch
):
    """With default config, a whole `sitt-collect` run with a real HTTP client.

    The guard stops NTES at the socket, which proves the run got as far as the network,
    and nothing in it resolved or connected to a Mobond or m-Indicator host. The adapter's
    fetch is also booby-trapped, so building a Mobond request at all fails the test.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("Mobond fetch was called with default configuration")

    monkeypatch.setattr(mobond, "fetch", forbidden)
    monkeypatch.setitem(SOURCES, mobond.SOURCE, (forbidden, mobond.parse_response))
    data_dir = default_environment / "data"

    exit_code = local.main(
        ["--data-dir", str(data_dir), "--db", str(default_environment / "sitt.duckdb")]
    )

    assert exit_code == 1  # NTES was blocked, so the run reports a failed source
    assert blocked_network and set(blocked_network) == {"enquiry.indianrail.gov.in"}
    assert not any("mobond" in host or "indicator" in host for host in blocked_network)
    assert not list(data_dir.rglob("*-mobond.json.gz"))


def test_a_mobond_request_is_only_built_when_asked_for(default_environment):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(503)

    with make_client(httpx.MockTransport(handler)) as client:
        local.run_once(
            default_environment / "data",
            default_environment / "sitt.duckdb",
            local.enabled_sources(),
            client,
        )
    assert hosts == ["enquiry.indianrail.gov.in"]
