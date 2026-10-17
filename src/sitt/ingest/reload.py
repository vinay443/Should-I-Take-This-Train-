"""Reload the timetable from Central Railway's PDFs in one command.

    uv run sitt-timetable-reload scratch            find the PDFs in `scratch`, back up the
                                                    database, convert and load them
    uv run sitt-timetable-reload scratch --dry-run  only say which PDFs were found

The PDFs are recognised by their SHA-256, not their names: the main line's DOWN and UP
editions and its AC and 15-car supplements (`cr_pdf.KNOWN_SOURCES`), and the Harbour line's
DOWN PDF (`HARBOUR_SOURCES` below). Both main line PDFs are needed; a supplement that isn't
there is left out with a warning; the Harbour line is loaded only if its PDF is there.

Before the timetable is touched, a copy of the database is made beside it
(`sitt-before-reload-<time>.duckdb`; the check that nothing else has the database brings
its schema up to date first, as opening it for writing always does), and nothing is changed
if the database is in use (stop `sitt-web`, the bot or the dashboard first). Every PDF is
converted and every CSV checked before anything is loaded, so a bad file stops the reload
with the timetable as it was. Readings, crowd reports and everything
else that isn't the timetable are kept: each line is loaded with `--replace`, which only
drops that line's trains that are no longer in its timetable.
"""

import argparse
import hashlib
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sitt.ingest import cr_pdf

# The Harbour line PDFs, recognised by hash: (edition, line). Kept apart from
# cr_pdf.KNOWN_SOURCES, which the dashboard downloads and converts as the main line.
HARBOUR_SOURCES: dict[str, tuple[str, str]] = {
    # "DN HB REVISED PTT WEF 01.05.2026.pdf"
    "7774ae4aacc1d0ccf5d07ce10f557c37fdcac0550385f7e856d6c934d97a8a78": ("2026-05-01", "harbour"),
}

# The main line's DOWN and UP editions, in that order (both kind "main" in KNOWN_SOURCES).
MAIN_ORDER = (
    "b5d8d8648af1c828b35d20920dc72ffb70f911b85585b9db711fd01a52b63c95",  # DOWN
    "68f5236a46352675c5f5067ac8c05614f982fa1c86b224eedb3af5e1c64ede2f",  # UP
)


@dataclass
class Found:
    """The known PDFs in a folder, by what they are."""

    main: list[Path] = field(default_factory=list)  # DOWN then UP
    ac: Path | None = None
    cars: Path | None = None
    harbour: Path | None = None
    harbour_edition: str | None = None
    unknown: list[Path] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"main line DOWN and UP: {', '.join(p.name for p in self.main) or 'not found'}"]
        out.append(f"AC supplement: {self.ac.name if self.ac else 'not found'}")
        out.append(f"15-car supplement: {self.cars.name if self.cars else 'not found'}")
        out.append(f"Harbour line DOWN: {self.harbour.name if self.harbour else 'not found'}")
        return out


def find_pdfs(folder: Path) -> Found:
    """Every known timetable PDF under `folder`, matched by SHA-256. Copies of the same
    file are fine: the first one found (in path order) is used."""
    found = Found()
    main: dict[str, Path] = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            found.unknown.append(path)  # can't be read, so it can't be one of ours
            continue
        if digest in cr_pdf.KNOWN_SOURCES:
            kind = cr_pdf.KNOWN_SOURCES[digest][2]
            if kind == "main":
                main.setdefault(digest, path)
            elif kind == "ac":
                found.ac = found.ac or path
            elif kind == "15car":
                found.cars = found.cars or path
        elif digest in HARBOUR_SOURCES:
            if found.harbour is None:
                found.harbour = path
                found.harbour_edition = HARBOUR_SOURCES[digest][0]
        else:
            found.unknown.append(path)
    found.main = [main[d] for d in MAIN_ORDER if d in main]
    return found


def _counts(db: Path) -> dict[str, int]:
    """Trains per line, and the readings and crowd reports kept alongside."""
    import duckdb

    with duckdb.connect(str(db), read_only=True) as con:
        out = dict(con.execute("SELECT line, count(*) FROM trains GROUP BY line").fetchall())
        out["readings"] = con.execute("SELECT count(*) FROM observations").fetchone()[0]
        out["crowd reports"] = con.execute("SELECT count(*) FROM crowd_reports").fetchone()[0]
    return out


def backup(db: Path, now: datetime | None = None) -> Path:
    """Copy the database (and its write-ahead log, if any) beside it. Never overwrites."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = db.with_name(f"sitt-before-reload-{stamp}{db.suffix}")
    n = 1
    while target.exists():
        n += 1
        target = db.with_name(f"sitt-before-reload-{stamp}-{n}{db.suffix}")
    shutil.copy2(db, target)
    wal = db.with_name(db.name + ".wal")
    if wal.exists():
        shutil.copy2(wal, target.with_name(target.name + ".wal"))
    return target


def _convert(paths, line: str, out: Path, edition=None, ac=None, cars=None) -> dict[str, str]:
    """Convert to a CSV at `out`. Returns the trains left out, with why."""
    conversion, sources = cr_pdf.convert(paths, edition, ac_supplement=ac, cars_supplement=cars)
    with out.open("w", encoding="utf-8", newline="") as f:
        cr_pdf.write_csv(
            f, conversion.trains, sources, overrides=len(conversion.overrides), line=line
        )
    return conversion.rejected


def left_out_lines(rejected: dict[str, str], limit: int = 5) -> list[str]:
    """Why trains were left out, grouped: "37 trains: 'TNA' (Thane) in the CSMT row: …"."""
    groups: dict[str, list[str]] = {}
    for number, reason in rejected.items():
        groups.setdefault(re.sub(r"^page \d+: ", "", reason), []).append(number)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    lines = [
        f"{len(numbers)} train{'s' if len(numbers) != 1 else ''}"
        f"{'' if len(numbers) > 3 else ' (' + ', '.join(sorted(numbers)) + ')'}: {why}"
        for why, numbers in ordered[:limit]
    ]
    if len(ordered) > limit:
        lines.append(f"and {len(ordered) - limit} more reasons")
    return lines


def reload(folder: Path, db: Path, *, dry_run: bool = False, out=print) -> int:
    """Find, back up, convert and load. Returns the exit code (0 done, 1 missing PDFs or a
    failed load, 2 database missing or busy)."""
    from sitt.db import DatabaseBusyError, open_with_retry
    from sitt.ingest.timetable import TimetableError, load_timetable, read_timetable

    found = find_pdfs(folder)
    out(f"Timetable PDFs in {folder}:")
    for line in found.lines():
        out(f"  {line}")
    if len(found.main) < 2:
        out(
            "Both main line PDFs are needed (the DOWN and UP editions of 05.10.2024; their "
            "URLs are at the top of src/sitt/ingest/cr_pdf.py). Nothing was changed."
        )
        return 1
    for name, path in (("AC", found.ac), ("15-car", found.cars)):
        if path is None:
            out(f"  warning: no {name} supplement, so the main line is loaded without it")
    if dry_run:
        return 0
    if not db.exists():
        out(
            f"No database at {db.resolve()}. Run this from the project folder, or pass "
            "--db with the database's path."
        )
        return 2

    # Is anything else using the database? Find out before converting or copying.
    try:
        open_with_retry(db, attempts=2, wait_seconds=1.0).close()
    except DatabaseBusyError:
        out(
            f"{db} is in use. Stop sitt-web (Ctrl+C in its window), the bot and the "
            "dashboard, then run this again. Nothing was changed."
        )
        return 2

    import duckdb

    try:
        before = _counts(db)
        copy = backup(db)
    except (duckdb.Error, OSError):
        out(
            f"{db} became busy while it was being copied. Stop sitt-web, the bot and the "
            "dashboard, then run this again. Nothing was changed."
        )
        return 2
    out(f"Backed up the database to {copy}")

    with tempfile.TemporaryDirectory(prefix="sitt-reload-") as work:
        work = Path(work)
        plans = [("central", "main", found.main, None, found.ac, found.cars)]
        if found.harbour:
            plans.append(("harbour", "harbour", [found.harbour], found.harbour_edition, None, None))
        try:
            csvs = []
            for loader_line, pdf_line, paths, edition, ac, cars in plans:
                csv = work / f"{loader_line}.csv"
                rejected = _convert(paths, pdf_line, csv, edition, ac, cars)
                note = f"; {len(rejected)} trains left out:" if rejected else "."
                out(f"Converted the {cr_pdf.LINE_TITLES[pdf_line]}{note}")
                for line in left_out_lines(rejected):
                    out(f"  {line}")
                csvs.append((loader_line, csv))
            # Every timetable is read and checked before any is loaded.
            timetables = [(line, read_timetable(csv, line=line)) for line, csv in csvs]
        except (cr_pdf.ConversionError, TimetableError, OSError) as exc:
            out(f"error: {exc}")
            out("Nothing was loaded: the timetable is as it was.")
            return 1
        loaded: list[str] = []
        try:
            con = open_with_retry(db, attempts=2, wait_seconds=1.0)
            try:
                for loader_line, timetable in timetables:
                    result = load_timetable(con, timetable, replace=True)
                    loaded.append(loader_line)
                    removed = (
                        f", {result.removed_trains} no longer in it removed"
                        if result.removed_trains
                        else ""
                    )
                    out(f"Loaded {result.trains} trains for line {loader_line!r}{removed}.")
            finally:
                con.close()
        except (DatabaseBusyError, TimetableError, duckdb.Error, OSError) as exc:
            # Each line loads in one transaction, so a line either loaded fully or not at all.
            done = ", ".join(loaded) or "none"
            missing = ", ".join(line for line, _ in timetables if line not in loaded)
            out(f"error: {exc}")
            out(
                f"Lines loaded: {done}; not loaded: {missing}. To go back to the timetable "
                f"as it was, stop everything and copy {copy} over {db}."
            )
            return 1

    after = _counts(db)
    keys = [k for k in after if k not in ("readings", "crowd reports")] + [
        "readings",
        "crowd reports",
    ]
    out("Before → after: " + "; ".join(f"{k} {before.get(k, 0)} → {after[k]}" for k in keys))
    out("Restart sitt-web to use it.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from sitt.config import load_settings

    parser = argparse.ArgumentParser(
        prog="sitt-timetable-reload",
        description="Reload the timetable from Central Railway's PDFs found in a folder.",
    )
    parser.add_argument("folder", type=Path, help="folder holding the PDFs (searched in full)")
    parser.add_argument("--db", type=Path, help="database (default SITT_DB_PATH)")
    parser.add_argument("--dry-run", action="store_true", help="only list the PDFs found")
    args = parser.parse_args(argv)
    # Piped or redirected on Windows, output may not be UTF-8: never fail over a "→".
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not args.folder.is_dir():
        print(f"No folder {args.folder}.")
        return 1
    db = args.db or load_settings().db_path
    return reload(args.folder, Path(db), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
