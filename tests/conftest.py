import socket
from pathlib import Path

import pytest

from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable

SAMPLE_CSV = Path(__file__).parent / "fixtures" / "sample_timetable.csv"

# Tests never touch the network. The guard below turns that from a convention into a
# failure: any attempt to resolve or connect to a host other than this machine raises,
# and the test that made the attempt fails even if the code under test swallowed the error.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


class NetworkBlockedError(OSError):
    """Raised instead of opening a real network connection during tests."""


def _host_of(address) -> str | None:
    """The host in a socket address, or None for addresses that aren't (host, port)."""
    if isinstance(address, tuple) and address and isinstance(address[0], str | bytes):
        host = address[0]
        return host.decode() if isinstance(host, bytes) else host
    return None  # e.g. a Unix socket path: local by definition


@pytest.fixture(autouse=True)
def blocked_network(request, monkeypatch):
    """Block real network access; yield the list of hosts a test tried to reach.

    A test that expects its code to be stopped here (to prove which hosts it does and
    doesn't try) is marked `@pytest.mark.expects_blocked_network`. Any other test fails
    if this list is not empty when it finishes.
    """
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(host: str | None) -> None:
        if host is not None and host.lower() not in _LOCAL_HOSTS:
            attempts.append(host)
            raise NetworkBlockedError(f"tests must not use the network (tried {host})")

    def connect(self, address):
        check(_host_of(address))
        return real_connect(self, address)

    def connect_ex(self, address):
        check(_host_of(address))
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        check(host.decode() if isinstance(host, bytes) else host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield attempts
    if attempts and request.node.get_closest_marker("expects_blocked_network") is None:
        pytest.fail(f"test tried to reach the network: {sorted(set(attempts))}")


@pytest.fixture
def con():
    with init_db(":memory:") as con:
        yield con


@pytest.fixture
def loaded(con):
    """A database holding the sample timetable (invented Kalyan-CSMT trains)."""
    load_timetable(con, read_timetable(SAMPLE_CSV))
    return con
