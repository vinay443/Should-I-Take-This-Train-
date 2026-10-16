"""`sitt-web` startup messages (sitt.web.run). No network: the address lookups are faked."""

from sitt.web import run


def test_home_network_addresses_best_first_without_duplicates():
    addresses = run.lan_addresses(
        hostname_lookup=lambda: ["127.0.1.1", "192.168.1.23", "172.20.0.5", "fe80::1"],
        route_lookup=lambda: ["192.168.1.23"],
    )
    assert addresses == ["192.168.1.23", "172.20.0.5"]


def test_addresses_a_phone_cannot_reach_are_left_out():
    addresses = run.lan_addresses(
        hostname_lookup=lambda: ["127.0.0.1", "169.254.3.4", "192.0.2.2", "8.8.4.4", "x"],
        route_lookup=lambda: ["10.0.0.7"],
    )
    assert addresses == ["10.0.0.7"]


def test_a_failing_lookup_is_skipped():
    def broken():
        raise OSError("no network")

    assert run.lan_addresses(hostname_lookup=broken, route_lookup=broken) == []
    assert run.lan_addresses(hostname_lookup=lambda: ["192.168.0.4"], route_lookup=broken) == [
        "192.168.0.4"
    ]


def test_phone_hint():
    hint = run.phone_hint(["192.168.1.23", "10.0.0.7"], 8000)
    assert "http://192.168.1.23:8000/ or http://10.0.0.7:8000/" in hint
    assert "firewall" in hint
    assert "ipconfig" in run.phone_hint([], 8080) and ":8080/" in run.phone_hint([], 8080)


def test_the_hint_is_printed_only_when_listening_for_the_phone(monkeypatch, capsys, tmp_path):
    import uvicorn

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(run, "lan_addresses", lambda: ["192.168.1.23"])
    run.main(["--no-browser", "--db", str(tmp_path / "x.duckdb")])
    assert "phone" not in capsys.readouterr().out
    run.main(["--no-browser", "--host", "0.0.0.0", "--db", str(tmp_path / "x.duckdb")])
    assert "open http://192.168.1.23:8000/" in capsys.readouterr().out
