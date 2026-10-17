"""Check the Delay patterns page's weekday/weekend switch in a real (headless) Chromium.

    python3 scripts/check_web_patterns.py [--out DIR]

Needs Playwright with Chromium for whichever python3 runs it (not a project dependency).
Builds two throwaway databases from the sample timetable plus invented readings: one with
readings on too few weekend days (the switch must stay hidden and the caveat must say what
is missing), and one with enough of each (the switch must appear, change the numbers, keep
the choice in the address and survive a reload). Starts `uv run sitt-web` itself on a free
port and never touches the network beyond 127.0.0.1. Exits 1 if a check fails.
"""

import argparse
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check_web_offline import (  # noqa: E402
    ROOT,
    SIZES,
    check,
    failures,
    free_port,
    sample_db,
    server,
)

READINGS = """
import sys
from datetime import date, datetime, time, timedelta

import duckdb

from sitt.tz import IST

con = duckdb.connect(sys.argv[1])
for iso in sys.argv[2:]:
    day = date.fromisoformat(iso)
    weekend = day.weekday() >= 5
    for train, hour, minute, delay in [
        ("central-90106", 7, 12, 9),
        ("central-90104", 7, 3, 2),
        ("central-90110", 7, 48, 0),
        ("central-90103", 7, 21, 4),
    ]:
        delay = 0 if weekend else delay
        expected = datetime.combine(day, time(hour, minute), IST) + timedelta(minutes=delay)
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, "
            "actual_or_expected_time, time_kind, delay_minutes, source, event) "
            "VALUES (?, ?, 'KYN', ?, 'expected', ?, 'ntes', 'departure')",
            [expected - timedelta(minutes=3), train, expected, delay],
        )
con.close()
"""


# A past week with no holiday in it: Monday to Thursday, then the weekend.
WEEKDAYS = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
WEEKEND = [date(2026, 9, 26), date(2026, 9, 27)]


def demo_db(folder: Path, days: list[date]) -> Path:
    folder.mkdir()
    db = sample_db(folder)
    add_readings(db, days)
    return db


def add_readings(db: Path, days: list[date]) -> None:
    """Invented runs at Kalyan on `days`: late on weekdays, on time at weekends. Runs in the
    project's environment (uv), which has DuckDB."""
    subprocess.run(
        ["uv", "run", "python", "-c", READINGS, str(db), *(d.isoformat() for d in days)],
        cwd=ROOT,
        check=True,
    )


def not_ready(browser, url: str) -> None:
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "patterns")
    page.wait_for_selector("#content:not([hidden])")
    check(page.is_hidden("#days-pick"), "too few weekend days: no Days switch")
    caveat = page.text_content("#caveat")
    check(
        "so far 4 weekdays and 1 weekend day or holiday" in caveat,
        f"caveat says what is missing ({caveat!r})",
    )
    check(not errors, f"no script errors ({errors})")
    page.close()


def ready(browser, url: str, out: Path) -> None:
    for name, (width, height) in SIZES.items():
        for scheme in ("light", "dark"):
            context = browser.new_context(
                viewport={"width": width, "height": height}, color_scheme=scheme
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            tag = f"{name}/{scheme}"
            page.goto(url + "patterns")
            page.wait_for_selector("#days-pick:not([hidden])")
            labels = page.locator("#days-toggle label").all_text_contents()
            check(
                [label.strip() for label in labels]
                == ["All days", "Weekdays", "Weekends & holidays"],
                f"{tag}: three choices ({labels})",
            )
            check(page.is_checked("input[name=days][value=all]"), f"{tag}: all days first")
            check("24 trains seen" in page.text_content("#summary"), f"{tag}: all-days summary")
            page.click("#days-toggle label:has(input[value=weekend])")
            summary = page.text_content("#summary")
            check(
                summary.startswith("Weekends & holidays: 8 trains seen"),
                f"{tag}: weekend summary ({summary!r})",
            )
            check("ON TIME" in page.text_content("#verdict-board"), f"{tag}: weekend verdict")
            check(page.url.endswith("/patterns?days=weekend"), f"{tag}: choice in the address")
            caveat = page.text_content("#caveat")
            check(
                "Monsoon days aren't told apart yet" in caveat and "shown apart once" not in caveat,
                f"{tag}: caveat no longer says weekdays aren't told apart",
            )
            # Wide enough to fit, no sideways scrolling.
            check(
                page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                f"{tag}: no sideways scrolling",
            )
            page.screenshot(path=out / f"patterns-weekend-{name}-{scheme}.png", full_page=True)
            page.reload()
            page.wait_for_selector("#days-pick:not([hidden])")
            check(
                page.is_checked("input[name=days][value=weekend]")
                and page.text_content("#summary").startswith("Weekends & holidays"),
                f"{tag}: reload keeps weekends",
            )
            page.click("#days-toggle label:has(input[value=weekday])")
            check(
                page.text_content("#summary").startswith("Weekdays: 16 trains seen")
                and "LATE" in page.text_content("#verdict-board"),
                f"{tag}: weekdays",
            )
            page.click("#days-toggle label:has(input[value=all])")
            check(page.url.endswith("/patterns"), f"{tag}: all days clears the address")
            check(not errors, f"{tag}: no script errors ({errors})")
            context.close()


def main() -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "sitt-shots")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as p:
        browser = p.chromium.launch()
        few = demo_db(Path(folder) / "few", [*WEEKDAYS, WEEKEND[0]])
        with server(few, free_port()) as (_, url):
            print("Too few weekend days:")
            not_ready(browser, url)
        enough = demo_db(Path(folder) / "enough", [*WEEKDAYS, *WEEKEND])
        with server(enough, free_port()) as (_, url):
            print("Enough of each:")
            ready(browser, url, args.out)
        browser.close()
    print(f"screenshots in {args.out}")
    print("all checks passed" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
