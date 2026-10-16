"""`sitt-web`: start the web app on this computer and open it in the browser.

    uv run sitt-web                    http://127.0.0.1:8000
    uv run sitt-web --port 8080 --no-browser
    uv run sitt-web --host 0.0.0.0     reachable from your phone on the same Wi-Fi

It runs until you stop it (Ctrl+C). Nothing is collected or scheduled in the background:
live status is fetched only when you press "Check live" on the page.
"""

import argparse
import ipaddress
import socket
import threading
import webbrowser
from pathlib import Path

# Home and office network ranges (RFC 1918). Not "private" in Python's wider sense, which
# also covers documentation and loopback addresses a phone can't reach.
HOME_NETWORKS = [
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
]


def lan_addresses(hostname_lookup=None, route_lookup=None) -> list[str]:
    """This computer's addresses on the home network (private IPv4), best first.

    Uses only local lookups: the addresses of this computer's name, and the address the
    system would send from (a UDP socket "connected" to a public address sends nothing).
    The two lookups can be replaced in tests.
    """
    found: list[str] = []
    for lookup in (route_lookup or _route_address, hostname_lookup or _hostname_addresses):
        try:
            candidates = lookup()
        except OSError:
            continue
        for text in candidates:
            try:
                address = ipaddress.ip_address(text)
            except ValueError:
                continue
            usable = any(address in network for network in HOME_NETWORKS)
            if usable and text not in found:
                found.append(text)
    return found


def _route_address() -> list[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # TEST-NET address; UDP connect sends no packet
        return [probe.getsockname()[0]]


def _hostname_addresses() -> list[str]:
    return socket.gethostbyname_ex(socket.gethostname())[2]


def phone_hint(addresses: list[str], port: int) -> str:
    if not addresses:
        return (
            "On your phone (same Wi-Fi), open http://<this PC's address>:"
            f"{port}/ (`ipconfig` shows the address)."
        )
    urls = " or ".join(f"http://{address}:{port}/" for address in addresses)
    return (
        f"On your phone (same Wi-Fi), open {urls}. If it doesn't load, allow Python "
        "through the Windows firewall for private networks."
    )


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from sitt.web.app import create_app

    parser = argparse.ArgumentParser(prog="sitt-web", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, help="database (default SITT_DB_PATH)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = parser.parse_args(argv)

    app = create_app(db_path=args.db)
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/"
    print(f"Should I take this train? is running at {url} (Ctrl+C to stop)")
    if args.host == "0.0.0.0":
        print(phone_hint(lan_addresses(), args.port))
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
