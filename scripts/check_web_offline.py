"""Check the web app in a real (headless) Chromium: installable, and usable when the app
on the PC stops. Takes screenshots at phone and desktop width, light and dark.

    python3 scripts/check_web_offline.py [--out DIR] [--db PATH]

Needs Playwright with Chromium for whichever python3 runs it (not a project dependency):
`pip install playwright && playwright install chromium`. Without --db it builds a
throwaway database from the sample timetable. It starts `uv run sitt-web` itself on a free
port, stops it halfway to imitate the PC going away, and never touches the network beyond
127.0.0.1. Exits 1 if a check fails.
"""

import argparse
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "sample_timetable.csv"
SIZES = {"phone": (390, 844), "desktop": (1280, 900)}

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sample_db(folder: Path) -> Path:
    db = folder / "sitt.duckdb"
    env = os.environ | {"SITT_DB_PATH": str(db)}
    subprocess.run(["uv", "run", "sitt-init-db"], cwd=ROOT, env=env, check=True)
    subprocess.run(
        ["uv", "run", "python", "-m", "sitt.ingest.timetable", str(SAMPLE), "--db", str(db)],
        cwd=ROOT,
        env=env,
        check=True,
    )
    return db


@contextlib.contextmanager
def server(db: Path, port: int):
    process = subprocess.Popen(
        ["uv", "run", "sitt-web", "--db", str(db), "--port", str(port), "--no-browser"],
        cwd=ROOT,
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        for _ in range(100):
            with contextlib.suppress(OSError):
                urllib.request.urlopen(url + "api/status", timeout=1)
                break
            time.sleep(0.2)
        else:
            raise SystemExit("sitt-web did not start")
        yield process, url
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(10)


def stop(process: subprocess.Popen) -> None:
    process.terminate()
    process.wait(10)


def run(db: Path, out: Path) -> None:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, (width, height) in SIZES.items():
            for scheme in ("light", "dark"):
                print(f"{name} {width}px, {scheme}")
                port = free_port()  # a fresh origin each time: no worker from the last run
                with server(db, port) as (process, url):
                    context = browser.new_context(
                        viewport={"width": width, "height": height},
                        color_scheme=scheme,
                        device_scale_factor=2 if name == "phone" else 1,
                        is_mobile=name == "phone",
                        has_touch=name == "phone",
                    )
                    page = context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                    page.goto(url)
                    page.wait_for_selector("#answer:not([hidden]) .trains li")
                    page.evaluate("navigator.serviceWorker.ready")
                    page.reload()  # now the worker controls the page
                    page.wait_for_selector("#answer:not([hidden]) .trains li")
                    controlled = page.evaluate("Boolean(navigator.serviceWorker.controller)")
                    check(controlled, "service worker controls the page")
                    if name == "phone" and scheme == "light":
                        cdp = context.new_cdp_session(page)
                        problems = cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
                        check(not problems, f"installable (Chrome says: {problems or 'no errors'})")
                        manifest = cdp.send("Page.getAppManifest")
                        check(not manifest.get("errors"), "manifest parses without errors")
                    check(page.is_hidden("#stale"), "no 'not live' note while the app answers")
                    page.screenshot(path=out / f"{name}-{scheme}-online.png", full_page=True)

                    # Other pages opened while online (the API docs, raw JSON) must never
                    # take the main page's place in the phone's copy.
                    page.goto(url + "api/docs")
                    errors.clear()  # the API docs load Swagger from a CDN, offline here
                    page.goto(url + "api/patterns")
                    stop(process)  # the PC goes away
                    page.goto(url)
                    check(
                        page.title() == "Should I take this train?",
                        f"offline, / is still the app after other pages ({page.title()!r})",
                    )
                    page.wait_for_selector("#stale:not([hidden])", timeout=10_000)
                    stale = page.inner_text("#stale")
                    check(stale.startswith("Not live."), f"saved answer marked: {stale[:60]}…")
                    check(page.locator(".trains li").count() > 0, "saved trains still listed")
                    line = page.inner_text("#data-line")
                    check(line.startswith("Offline"), f"header says offline: {line[:40]}…")
                    page.screenshot(path=out / f"{name}-{scheme}-offline.png", full_page=True)

                    # A trip with no saved answer says so instead of showing nothing useful.
                    page.fill("#to", "Thane (TNA)")
                    page.click("#go")
                    page.wait_for_selector("#message:not([hidden])")
                    check(
                        "no saved answer" in page.inner_text("#message"),
                        "unsaved trip offline gets a clear message",
                    )
                    check(not errors, f"no script errors ({errors})")
                    context.close()
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(tempfile.gettempdir()) / "sitt-web-offline",
        help="where the screenshots go (default: a folder in the system temp directory)",
    )
    parser.add_argument("--db", type=Path, help="database to use (a copy is safest)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        run(args.db or sample_db(Path(folder)), args.out)
    print(f"screenshots in {args.out}")
    print("all checks passed" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
