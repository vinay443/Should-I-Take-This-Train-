"""Planned engineering blocks ("megablocks"): fetch what is announced, or enter them by hand.

    sitt-block fetch                 read Yatri's announcements page (one request)
    sitt-block add --date 2026-10-11 --from MTN --to MLND --start 11:05 --end 15:55 --tracks fast
    sitt-block parse --date 2026-10-11 "Thane-Kalyan Up and Down slow lines from 10.40 to 15.40"
    sitt-block list [--from-date 2026-10-01]
    sitt-block remove BLOCK_ID
    sitt-block load DIR              import JSON files written by `fetch --json-out`

(`python -m sitt.ingest.blocks` works too.)

What can and can't be fetched
-----------------------------
As of October 2026 no public web page gives a megablock's section and times in a form
that can be fetched reliably:

- Yatri's announcements page (https://yatrirailways.com/live-railway-announcements, the
  official Mumbai local app's site) lists megablocks as cards, but a card carries only
  a title such as "Megablock on Central and Harbour line on Sunday, 27th September".
  The section, times and tracks are inside the app.
- Central Railway's press-releases page had no megablock notices when checked.
- The full notice appears on X and on third-party news pages, as prose that this
  project shouldn't copy.

So `fetch` records what Yatri does publish: **the date and the line**. Such a block has
no section, times or tracks, and the delay model treats it as "a megablock somewhere
on this line today". When you know the details (from the app or a notice), add them
with `add`, or paste the notice's wording into `parse`. A detailed block replaces the
date-only one for that day and line.

Only facts are stored: date, line, section, times, tracks, and a summary in this
project's own words. The announcement's text is never kept.
"""

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path

import duckdb
import httpx

from sitt.config import load_settings
from sitt.db import init_db
from sitt.ingest.cr_pdf import station_for
from sitt.ingest.live.common import SourceError, make_client, request

YATRI_URL = "https://yatrirailways.com/live-railway-announcements"
YATRI_SOURCE = "yatri"
MANUAL_SOURCE = "manual"

LINES = ("central", "harbour", "transharbour", "western")
TRACKS = ("fast", "slow", "both")
DIRECTIONS = ("up", "down", "both")

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}


@dataclass(frozen=True)
class Block:
    """One planned block. `None` means the announcement didn't say."""

    block_date: date
    line: str
    from_station: str | None = None
    to_station: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    tracks: str | None = None
    direction: str | None = None
    source: str = MANUAL_SOURCE
    summary: str | None = None

    @property
    def block_id(self) -> str:
        key = "|".join(
            str(part or "")
            for part in (
                self.block_date,
                self.line,
                self.from_station,
                self.to_station,
                self.start_time,
                self.end_time,
                self.tracks,
                self.direction,
            )
        )
        return f"{self.block_date:%Y%m%d}-{self.line}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"

    @property
    def detailed(self) -> bool:
        """Whether it says where or when, not just which day and line."""
        return any((self.from_station, self.to_station, self.start_time, self.end_time))

    def describe(self) -> str:
        parts = [f"{self.block_date:%a %d %b %Y}", self.line]
        if self.from_station or self.to_station:
            parts.append(f"{self.from_station or '?'}-{self.to_station or '?'}")
        if self.tracks:
            parts.append(f"{self.tracks} lines")
        if self.direction:
            parts.append(self.direction)
        if self.start_time or self.end_time:
            start = f"{self.start_time:%H:%M}" if self.start_time else "?"
            end = f"{self.end_time:%H:%M}" if self.end_time else "?"
            parts.append(f"{start}-{end}")
        if not self.detailed:
            parts.append("(section and times not announced on the web)")
        return ", ".join(parts)


class BlockError(ValueError):
    pass


# --- Stations -------------------------------------------------------------------------


def station_code(text: str | None) -> str | None:
    """A station code where the name is a known Central main line station, else the text."""
    if text is None or not text.strip():
        return None
    cleaned = re.sub(r"\s+", " ", text.strip())
    station = station_for(cleaned)
    return station.code if station else cleaned


# --- Yatri's announcements page ----------------------------------------------------------


@dataclass(frozen=True)
class Announcement:
    category: str  # the card's class: '2' is the "Mega Blocks" filter
    title: str
    posted: str
    teaser: str


class _CardParser(HTMLParser):
    """Collects the announcement cards: a numbered `div` holding a `card-box` of `<p>`s."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[Announcement] = []
        self._category: str | None = None
        self._texts: list[str] | None = None
        self._in_p = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "div":
            if self._texts is not None:
                self._depth += 1
            elif "card-box" in classes and self._category is not None:
                self._texts, self._depth = [], 0
            elif classes and classes[0].isdigit() and any(c.startswith("col-") for c in classes):
                self._category = classes[0]
        elif tag == "p" and self._texts is not None:
            self._in_p = True
            self._texts.append("")

    def handle_endtag(self, tag):
        if tag == "p":
            self._in_p = False
        elif tag == "div" and self._texts is not None:
            if self._depth:
                self._depth -= 1
                return
            texts = [re.sub(r"\s+", " ", t).strip() for t in self._texts] + ["", "", ""]
            self.cards.append(Announcement(self._category or "", *texts[:3]))
            self._texts, self._category = None, None

    def handle_data(self, data):
        if self._in_p and self._texts:
            self._texts[-1] += data


def parse_announcements(html_text: str) -> list[Announcement]:
    parser = _CardParser()
    parser.feed(html_text)
    return parser.cards


_POSTED_RE = re.compile(r"(\d{1,2}) ([A-Za-z]{3,9}) (\d{2,4})")
_DAY_MONTH_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?(?:\s*/\s*\d{1,2}(?:st|nd|rd|th)?)?\s+([A-Za-z]{3,9})", re.I
)
_BLOCK_WORD_RE = re.compile(r"\b(?:mega|jumbo|special|night)?\s*-?\s*block\b", re.I)


def _posted_date(posted: str) -> date | None:
    """'Sat, 26 Sep 26 10:55AM' -> 2026-09-26."""
    match = _POSTED_RE.search(posted)
    if not match or match[2].lower() not in _MONTHS:
        return None
    year = int(match[3])
    try:
        return date(year + 2000 if year < 100 else year, _MONTHS[match[2].lower()], int(match[1]))
    except ValueError:
        return None


def _block_date(title: str, posted: date) -> date | None:
    """The first date named in the title, in the year that puts it nearest after `posted`."""
    for match in _DAY_MONTH_RE.finditer(title):
        month = _MONTHS.get(match[2].lower())
        if month is None:
            continue
        for year in (posted.year, posted.year + 1):
            try:
                day = date(year, month, int(match[1]))
            except ValueError:
                continue
            if day >= posted - timedelta(days=7):
                return day
    return None


def _lines_in(title: str) -> list[str]:
    text = title.lower()
    lines = []
    if re.search(r"trans[- ]?harbour", text):
        lines.append("transharbour")
        text = re.sub(r"trans[- ]?harbour", " ", text)
    if "harbour" in text:
        lines.append("harbour")
    if "western" in text:
        lines.append("western")
    if re.search(r"\bcentral\b|\bmain line\b", text):
        lines.insert(0, "central")
    return lines


def blocks_from_announcements(
    announcements: Sequence[Announcement],
) -> tuple[list[Block], list[str]]:
    """Date-and-line blocks from Yatri's cards. Returns (blocks, warnings)."""
    blocks: list[Block] = []
    warnings: list[str] = []
    for card in announcements:
        if not (card.category == "2" or _BLOCK_WORD_RE.search(card.title)):
            continue
        posted = _posted_date(card.posted)
        if posted is None:
            warnings.append(f"can't read when this was posted ({card.posted!r}); skipped")
            continue
        day = _block_date(card.title, posted)
        lines = _lines_in(card.title)
        if day is None or not lines:
            missing = "date" if day is None else "line"
            warnings.append(f"a block announcement posted {posted} has no readable {missing}")
            continue
        for line in lines:
            blocks.append(
                Block(
                    block_date=day,
                    line=line,
                    source=YATRI_SOURCE,
                    summary=f"Megablock announced on Yatri on {posted}; details only in the app",
                )
            )
    return blocks, warnings


def fetch_yatri(client: httpx.Client) -> tuple[list[Block], list[str], list[str]]:
    """One GET of Yatri's page. Returns (blocks, warnings, request log)."""
    log: list[str] = []
    response = request(client, "GET", YATRI_URL, log)
    announcements = parse_announcements(response.text)
    blocks, warnings = blocks_from_announcements(announcements)
    if not announcements:
        warnings.append("no announcement cards found; the page layout may have changed")
    return blocks, warnings, log


# --- Announcement wording ----------------------------------------------------------------

_TIME = r"(\d{1,2})[.:](\d{2})\s*(am|pm|a\.m\.|p\.m\.|hrs|hours)?"
_NOTICE_RE = re.compile(
    rf"""
    (?P<from>[A-Za-z][A-Za-z. ]*?)\s*(?:-|–|to)\s*(?P<to>[A-Za-z][A-Za-z. ]*?)\s+
    (?P<direction>up\s*(?:and|&)\s*(?:down|dn)|(?:down|dn)\s*(?:and|&)\s*up|up|down|dn)\s+
    (?P<tracks>fast|slow|harbour|trans[- ]?harbour)?\s*lines?\s+
    from\s+(?P<start>{_TIME})\s+to\s+(?P<end>{_TIME})
    """,
    re.I | re.X,
)


_LEADING_WORDS_RE = re.compile(r"^(?:(?:and|also|on|the|between)\s+)+", re.I)


def _clock(text: str) -> time:
    match = re.fullmatch(_TIME, text.strip(), re.I)
    hour, minute, suffix = int(match[1]), int(match[2]), (match[3] or "").lower().replace(".", "")
    if suffix in ("am", "pm"):
        if not 1 <= hour <= 12:
            raise BlockError(f"can't read the time {text!r}")
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    if hour > 23 or minute > 59:
        raise BlockError(f"can't read the time {text!r}")
    return time(hour, minute)


def parse_notice(text: str, block_date: date, line: str = "central") -> list[Block]:
    """Blocks described in a notice's usual wording.

    Understands sentences like "Matunga-Mulund Up and Down fast lines from 11.05 am to
    3.55 pm". A harbour or trans-harbour notice sets the line. Anything else is ignored,
    so check the result. The text itself is not stored.
    """
    blocks = []
    for match in _NOTICE_RE.finditer(text):
        direction = match["direction"].lower()
        both = "and" in direction or "&" in direction
        tracks = (match["tracks"] or "").lower().replace(" ", "").replace("-", "")
        block_line = tracks if tracks in ("harbour", "transharbour") else line
        blocks.append(
            Block(
                block_date=block_date,
                line=block_line,
                from_station=station_code(_LEADING_WORDS_RE.sub("", match["from"])),
                to_station=station_code(match["to"]),
                start_time=_clock(match["start"]),
                end_time=_clock(match["end"]),
                tracks=tracks if tracks in ("fast", "slow") else None,
                direction="both" if both else ("up" if direction == "up" else "down"),
                source=MANUAL_SOURCE,
                summary="Entered from a megablock notice",
            )
        )
    return blocks


# --- Storage -----------------------------------------------------------------------------

_INSERT = (
    "INSERT INTO blocks (block_id, block_date, line, from_station, to_station, start_time, "
    "end_time, tracks, direction, source, summary, recorded_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (block_id) DO NOTHING"
)


def save_blocks(con: duckdb.DuckDBPyConnection, blocks: Sequence[Block]) -> int:
    """Store blocks, skipping ones already there. Returns how many were added.

    A detailed block replaces date-only blocks for the same day and line, and a
    date-only block isn't added where a detailed one exists.
    """
    added = 0
    for block in blocks:
        same_day = con.execute(
            "SELECT block_id, from_station IS NOT NULL OR to_station IS NOT NULL "
            "OR start_time IS NOT NULL OR end_time IS NOT NULL FROM blocks "
            "WHERE block_date = ? AND line = ?",
            [block.block_date, block.line],
        ).fetchall()
        if block.detailed:
            vague = [block_id for block_id, detailed in same_day if not detailed]
            for block_id in vague:
                con.execute("DELETE FROM blocks WHERE block_id = ?", [block_id])
        elif any(detailed for _, detailed in same_day):
            continue
        if any(block_id == block.block_id for block_id, _ in same_day):
            continue
        con.execute(
            _INSERT,
            [
                block.block_id,
                block.block_date,
                block.line,
                block.from_station,
                block.to_station,
                block.start_time,
                block.end_time,
                block.tracks,
                block.direction,
                block.source,
                block.summary,
                datetime.now(UTC),
            ],
        )
        added += 1
    return added


def list_blocks(con: duckdb.DuckDBPyConnection, from_date: date | None = None) -> list[Block]:
    rows = con.execute(
        "SELECT block_date, line, from_station, to_station, start_time, end_time, tracks, "
        "direction, source, summary FROM blocks WHERE $day IS NULL OR block_date >= $day "
        "ORDER BY block_date, line, start_time",
        {"day": from_date},
    ).fetchall()
    return [Block(*row) for row in rows]


def blocks_to_json(blocks: Sequence[Block]) -> str:
    rows = []
    for block in blocks:
        row = asdict(block)
        for key in ("block_date", "start_time", "end_time"):
            row[key] = row[key].isoformat() if row[key] is not None else None
        rows.append(row)
    return json.dumps(rows, indent=1) + "\n"


def blocks_from_json(text: str) -> list[Block]:
    blocks = []
    for row in json.loads(text):
        row["block_date"] = date.fromisoformat(row["block_date"])
        for key in ("start_time", "end_time"):
            row[key] = time.fromisoformat(row[key]) if row.get(key) else None
        blocks.append(Block(**row))
    return blocks


# --- CLI ---------------------------------------------------------------------------------


def _time_arg(text: str) -> time:
    try:
        return _clock(text)
    except (BlockError, AttributeError):
        raise argparse.ArgumentTypeError(f"not a time: {text!r} (use HH:MM)") from None


def _validated(block: Block) -> Block:
    if block.line not in LINES:
        raise BlockError(f"line must be one of {', '.join(LINES)}")
    if block.tracks is not None and block.tracks not in TRACKS:
        raise BlockError(f"tracks must be one of {', '.join(TRACKS)}")
    if block.direction is not None and block.direction not in DIRECTIONS:
        raise BlockError(f"direction must be one of {', '.join(DIRECTIONS)}")
    return block


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sitt-block", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--db", type=Path, help="database (default: SITT_DB_PATH or data/sitt.duckdb)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="read Yatri's announcements page (one request)")
    fetch.add_argument("--dry-run", action="store_true", help="print what was found, store nothing")
    fetch.add_argument("--json-out", type=Path, help="also write the blocks to this JSON file")
    fetch.add_argument("--no-db", action="store_true", help="don't touch the database")

    add = commands.add_parser("add", help="enter a block by hand")
    add.add_argument("--date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    add.add_argument("--line", default="central", choices=LINES)
    add.add_argument("--from", dest="from_station", help="station code or name at one end")
    add.add_argument("--to", dest="to_station", help="station code or name at the other end")
    add.add_argument("--start", type=_time_arg, help="HH:MM")
    add.add_argument("--end", type=_time_arg, help="HH:MM (before --start means past midnight)")
    add.add_argument("--tracks", choices=TRACKS)
    add.add_argument("--direction", choices=DIRECTIONS)
    add.add_argument("--note", help="a short note in your own words")

    parse = commands.add_parser("parse", help="enter blocks from a notice's wording")
    parse.add_argument("--date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    parse.add_argument("--line", default="central", choices=LINES)
    parse.add_argument("--dry-run", action="store_true")
    parse.add_argument("text", help="the sentence(s) describing the block")

    show = commands.add_parser("list", help="show stored blocks")
    show.add_argument("--from-date", type=date.fromisoformat)

    remove = commands.add_parser("remove", help="delete a block by its ID")
    remove.add_argument("block_id")

    load = commands.add_parser("load", help="import JSON files written by `fetch --json-out`")
    load.add_argument("dir", type=Path)

    args = parser.parse_args(argv)
    db_path = args.db or load_settings().db_path

    def store(blocks: Sequence[Block]) -> int:
        with init_db(db_path) as con:
            return save_blocks(con, blocks)

    try:
        if args.command == "fetch":
            with make_client() as client:
                blocks, warnings, log = fetch_yatri(client)
            for line in log:
                print(line)
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            for block in blocks:
                print(f"  {block.describe()}")
            if args.json_out and not args.dry_run:
                args.json_out.parent.mkdir(parents=True, exist_ok=True)
                args.json_out.write_text(blocks_to_json(blocks), encoding="utf-8")
            if args.dry_run or args.no_db:
                print(f"Found {len(blocks)} block(s); database not changed.")
            else:
                print(f"Found {len(blocks)} block(s); {store(blocks)} new.")
        elif args.command == "add":
            block = _validated(
                Block(
                    block_date=args.date,
                    line=args.line,
                    from_station=station_code(args.from_station),
                    to_station=station_code(args.to_station),
                    start_time=args.start,
                    end_time=args.end,
                    tracks=args.tracks,
                    direction=args.direction,
                    source=MANUAL_SOURCE,
                    summary=args.note or "Entered by hand",
                )
            )
            added = store([block])
            print(
                f"{'Added' if added else 'Already stored'}: {block.describe()} [{block.block_id}]"
            )
        elif args.command == "parse":
            blocks = parse_notice(args.text, args.date, args.line)
            if not blocks:
                raise BlockError(
                    "couldn't find a block in that text. Expected wording like "
                    '"Matunga-Mulund Up and Down fast lines from 11.05 am to 3.55 pm". '
                    "Use `add` to enter it field by field."
                )
            for block in blocks:
                print(f"  {block.describe()}")
            if args.dry_run:
                print(f"Read {len(blocks)} block(s); database not changed.")
            else:
                print(f"Read {len(blocks)} block(s); {store(blocks)} new.")
        elif args.command == "list":
            with init_db(db_path) as con:
                blocks = list_blocks(con, args.from_date)
            for block in blocks:
                print(f"{block.block_id}  {block.describe()}  ({block.source})")
            print(f"{len(blocks)} block(s).")
        elif args.command == "remove":
            with init_db(db_path) as con:
                (count,) = con.execute(
                    "SELECT count(*) FROM blocks WHERE block_id = ?", [args.block_id]
                ).fetchone()
                con.execute("DELETE FROM blocks WHERE block_id = ?", [args.block_id])
            print("Removed." if count else f"No block with ID {args.block_id}.")
            return 0 if count else 1
        elif args.command == "load":
            files = sorted(args.dir.rglob("*.json")) if args.dir.is_dir() else []
            if not files:
                raise BlockError(f"no JSON files under {args.dir}")
            blocks = [b for f in files for b in blocks_from_json(f.read_text(encoding="utf-8"))]
            print(f"Read {len(blocks)} block(s) from {len(files)} file(s); {store(blocks)} new.")
    except (BlockError, SourceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
