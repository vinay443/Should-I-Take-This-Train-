"""`sitt-web` startup messages (sitt.web.run). No network: the address lookups are faked."""

from sitt.web import run


def test_home_network_addresses_best_first_without_duplicates():
    addresses = run.lan_addresses(
        hostname_lookup=lambda: ["127.0.1.1", "192.168.1.23", "172.20.0.5", "fe80::1"],
        route_lookup=lambda: ["192.168.1.23"],
    )
    # 172.20.0.5 from the name lookup is most likely a WSL/Hyper-V adapter: dropped.
    assert addresses == ["192.168.1.23"]


def test_wifi_comes_before_a_vpn_and_virtual_adapters_are_dropped():
    addresses = run.lan_addresses(
        route_lookup=lambda: ["10.8.0.2"],  # full-tunnel VPN takes the default route
        hostname_lookup=lambda: ["172.24.160.1", "10.8.0.2", "192.168.1.23"],
    )
    assert addresses == ["192.168.1.23", "10.8.0.2"]


def test_a_172_address_is_kept_when_it_is_all_there_is():
    assert run.lan_addresses(
        route_lookup=lambda: ["172.16.5.9"], hostname_lookup=lambda: ["172.24.160.1"]
    ) == ["172.16.5.9", "172.24.160.1"]


def test_addresses_a_phone_cannot_reach_are_left_out():
    addresses = run.lan_addresses(
        hostname_lookup=lambda: ["127.0.0.1", "169.254.3.4", "192.0.2.2", "8.8.4.4", "x"],
        route_lookup=lambda: ["10.0.0.7"],
    )
    assert addresses == ["10.0.0.7"]


def test_a_failing_lookup_is_skipped():
    def broken():
        raise OSError("no network")

    def long_name():
        raise UnicodeError("label too long")

    assert run.lan_addresses(hostname_lookup=long_name, route_lookup=broken) == []
    assert run.lan_addresses(hostname_lookup=broken, route_lookup=broken) == []
    assert run.lan_addresses(hostname_lookup=lambda: ["192.168.0.4"], route_lookup=broken) == [
        "192.168.0.4"
    ]


def test_phone_hint():
    hint = run.phone_hint(["192.168.1.23", "10.0.0.7"], 8000)
    assert (
        "open http://192.168.1.23:8000/ (if that doesn't load, try http://10.0.0.7:8000/)" in hint
    )
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


def test_local_url_host():
    assert run.local_host("0.0.0.0") == "127.0.0.1"
    assert run.local_host("::") == "[::1]"
    assert run.local_host("fe80::1") == "[fe80::1]"
    assert run.local_host("192.168.1.23") == "192.168.1.23"
