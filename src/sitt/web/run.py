"""`sitt-web`: start the web app on this computer and open it in the browser.

    uv run sitt-web                    http://127.0.0.1:8000
    uv run sitt-web --port 8080 --no-browser
    uv run sitt-web --host 0.0.0.0     reachable from your phone on the same Wi-Fi

It runs until you stop it (Ctrl+C). Nothing is collected or scheduled in the background:
live status is fetched only when you press "Check live" on the page.
"""

import argparse
import threading
import webbrowser
from pathlib import Path


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
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
