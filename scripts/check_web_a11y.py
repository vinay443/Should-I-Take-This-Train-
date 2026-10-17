"""Accessibility check of the web app's pages with axe-core, in a real (headless) Chromium.

    npm install --prefix <folder> axe-core@4.10.3
    python3 scripts/check_web_a11y.py --axe <folder>/node_modules/axe-core/axe.min.js [--db PATH]

Needs Playwright with Chromium for whichever python3 runs it (not a project dependency).
Without --db it builds a throwaway database from the sample timetable plus invented readings
over a week, so the Delay patterns page has something to show, its weekday/weekend switch
included. Checks the main page (with a train's details open), the offline "Not live" view
and Delay patterns (all days and weekends), at phone and desktop width, light and dark.
Exits 1 on any serious or critical violation; lists the rest.
"""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check_web_offline import free_port, sample_db, server, stop  # noqa: E402
from check_web_patterns import WEEKDAYS, WEEKEND, add_readings  # noqa: E402

SIZES = {"phone": (390, 844), "desktop": (1280, 900)}
BLOCKING = {"serious", "critical"}


def audit(page, axe: str, label: str, problems: list[str]) -> None:
    page.add_script_tag(content=axe)
    result = page.evaluate("axe.run(document)")
    # "Incomplete": axe couldn't decide (text over an overlapping tooltip, SVG text). Listed
    # so they can be checked by hand; not counted as violations.
    for item in result["incomplete"]:
        nodes = [node["target"][0] for node in item["nodes"]][:3]
        print(f"  {label}: (check by hand) {item['id']}: {len(item['nodes'])} node(s) {nodes}")
    for violation in result["violations"]:
        nodes = [node["target"][0] for node in violation["nodes"]][:4]
        line = f"{label}: [{violation['impact']}] {violation['id']}: {violation['help']} {nodes}"
        print("  " + line)
        if violation["impact"] in BLOCKING:
            problems.append(line)
    if not result["violations"]:
        print(f"  {label}: no violations")


def run(db: Path, axe: str) -> list[str]:
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, (width, height) in SIZES.items():
            for scheme in ("light", "dark"):
                port = free_port()
                with server(db, port) as (process, url):
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, color_scheme=scheme
                    )
                    page = context.new_page()
                    tag = f"{name}/{scheme}"
                    page.goto(url)
                    page.wait_for_selector("#answer:not([hidden]) .trains li")
                    page.click(".trains li button")  # open a train's details
                    page.click(".why summary")
                    audit(page, axe, f"{tag} main", problems)
                    page.goto(url + "patterns")
                    page.wait_for_selector("#content:not([hidden])")
                    page.click(".why summary")  # "How these numbers are worked out"
                    audit(page, axe, f"{tag} patterns", problems)
                    page.focus(".hour-chart .slot[tabindex='0']")  # with a tooltip showing
                    audit(page, axe, f"{tag} patterns+tooltip", problems)
                    if page.is_visible("#days-pick"):  # the weekday/weekend switch
                        page.click("#days-toggle label:has(input[value=weekend])")
                        audit(page, axe, f"{tag} patterns weekends", problems)
                    page.goto(url)
                    page.wait_for_selector("#answer:not([hidden]) .trains li")
                    page.evaluate("navigator.serviceWorker.ready")
                    page.reload()
                    page.wait_for_selector("#answer:not([hidden]) .trains li")
                    stop(process)
                    page.reload()
                    page.wait_for_selector("#stale:not([hidden])")
                    audit(page, axe, f"{tag} offline", problems)
                    context.close()
        browser.close()
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--axe", type=Path, required=True, help="path to axe.min.js")
    parser.add_argument("--db", type=Path, help="database to use (a copy is safest)")
    args = parser.parse_args()
    axe = args.axe.read_text("utf-8")
    with tempfile.TemporaryDirectory() as folder:
        db = args.db
        if db is None:
            db = sample_db(Path(folder))
            add_readings(db, [*WEEKDAYS, *WEEKEND])
        problems = run(db, axe)
    print("no serious or critical violations" if not problems else f"{len(problems)} blocking")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
