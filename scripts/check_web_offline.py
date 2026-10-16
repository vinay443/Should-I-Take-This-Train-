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
from datetime import UTC, datetime, timedelta
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


def saved_trains_leave(page, shot: Path) -> None:
    """Offline, move the clock on: trains that have left drop out of the saved answer."""
    answers = page.evaluate("JSON.parse(localStorage.getItem('answers'))")
    answer = max(answers.values(), key=lambda entry: entry["saved_at"])["answer"]
    shown = page.locator(".trains li").count()
    first = datetime.fromisoformat(answer["options"][0]["predicted_departure"])
    page.clock.set_system_time(first + timedelta(minutes=5))
    page.click("#go")
    page.wait_for_function(f"document.querySelectorAll('.trains li').length === {shown - 1}")
    notes = page.inner_text("#notes")
    check("1 train in the saved answer has left" in notes, f"departed train dropped: {notes!r}")
    page.screenshot(path=shot, full_page=True)
    page.clock.set_system_time(first + timedelta(days=3))
    page.click("#go")
    page.wait_for_function("document.querySelectorAll('.trains li').length === 0")
    reason = page.inner_text("#reason")
    check(reason.startswith("Every train in this saved answer"), f"all gone: {reason[:50]!r}")
    check(page.is_hidden(".why"), "explanation hidden once the picked train has left")
    page.clock.set_system_time(datetime.now(UTC))


def button_edge_cases(browser, url: str) -> None:
    """The live button with the app reachable: an erroring status check must not leave it
    stuck on "needs the PC", and it must stay disabled while a request is running.

    Without a service worker (as on a phone over plain http://): Playwright can't intercept
    requests a service worker makes, so faked answers would never reach the page."""
    context = browser.new_context(service_workers="block")
    page = context.new_page()
    page.goto(url)
    page.wait_for_selector("#answer:not([hidden]) .trains li")
    live = page.locator("#live")
    page.evaluate("setOffline(true)")
    page.route("**/api/status", lambda route: route.fulfill(status=500, body="{}"))
    page.click("#go")
    page.wait_for_function("!document.getElementById('go').disabled")
    page.wait_for_timeout(300)  # let the follow-up status check finish
    check(live.is_enabled(), "PC answers but its status check fails: live button usable")
    page.unroute("**/api/status")

    def slow(route):
        page.wait_for_timeout(1500)
        route.continue_()

    page.route("**/api/recommend*", slow)
    page.evaluate("setOffline(true)")
    page.click("#go")
    page.evaluate("window.dispatchEvent(new Event('online'))")  # re-checks the status
    page.wait_for_timeout(700)
    check(live.is_disabled(), "live button stays disabled while a request is running")
    page.wait_for_function("!document.getElementById('go').disabled")
    page.unroute("**/api/recommend*")
    check(live.is_enabled(), "and usable again afterwards")
    context.close()


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
                    page.clock.install()  # real time, but it can be moved forward later
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

                    live = page.locator("#live")
                    check(
                        live.is_disabled() and live.inner_text() == "Live check needs the PC",
                        f"offline, the live button says it needs the PC ({live.inner_text()!r})",
                    )
                    saved_trains_leave(page, out / f"{name}-{scheme}-offline-later.png")

                    # A trip with no saved answer says so instead of showing nothing useful.
                    page.fill("#to", "Thane (TNA)")
                    page.click("#go")
                    page.wait_for_selector("#message:not([hidden])")
                    check(
                        "no saved answer" in page.inner_text("#message"),
                        "unsaved trip offline gets a clear message",
                    )
                    # The PC comes back: asking again gets a live answer and the button back.
                    with server(db, port):
                        page.fill("#to", "CSMT")
                        page.click("#go")
                        page.wait_for_function(
                            "document.getElementById('live').textContent === 'Check live first'"
                        )
                        check(
                            live.is_enabled() and page.is_hidden("#stale"),
                            "back online: live button enabled, no 'not live' note",
                        )
                        button_edge_cases(browser, url)
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
