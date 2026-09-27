"""`/next`: timetable lookups, their formatting and the handler."""

import asyncio
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sitt.bot import formatting, handlers
from sitt.bot.flow import IST
from sitt.bot.schedule import Departures, ScheduleError, upcoming_trains
from sitt.db import init_db
from sitt.ingest.cr_pdf import SourceInfo, Word, grid_trains, parse_grid, write_csv
from sitt.ingest.timetable import load_timetable, read_timetable

FIXTURES = Path(__file__).parent.parent / "fixtures"
MONDAY_7AM = datetime(2026, 9, 28, 7, 0)  # sample timetable trains are invented


@pytest.fixture
def sample_db(tmp_path):
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(FIXTURES / "sample_timetable.csv"))
    return path


@pytest.fixture
def pdf_db(tmp_path):
    """A database holding the trains on the real PDF pages in cr_pdf_words.json."""
    pages = json.loads((FIXTURES / "cr_pdf_words.json").read_text(encoding="utf-8"))["pages"]
    grids = [parse_grid([Word(*w) for w in p["words"]], p["page"]) for p in pages]
    out = io.StringIO()
    write_csv(out, grid_trains(grids).trains, [SourceInfo("fixture", "-", None, None)])
    csv_path = tmp_path / "central.csv"
    csv_path.write_text(out.getvalue(), encoding="utf-8")
    path = tmp_path / "pdf.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(csv_path))
    return path


def test_upcoming_trains(sample_db):
    departures = upcoming_trains(sample_db, "kyn", "CSMT", MONDAY_7AM, n=2)
    assert (departures.origin, departures.destination) == (
        "Kalyan",
        "Chhatrapati Shivaji Maharaj Terminus",
    )
    assert [(t.number, f"{t.departure:%H:%M}") for t in departures.trips] == [
        ("90104", "07:03"),
        ("90106", "07:12"),
    ]


def test_bot_aliases_resolve(sample_db):
    departures = upcoming_trains(sample_db, "Kalyan", "vt", MONDAY_7AM, n=1)
    assert departures.trips[0].number == "90104"


def test_station_whose_code_differs_resolves_by_name(pdf_db):
    # The bot knows Sion as SIN; the PDF timetable uses the NTES code SION.
    departures = upcoming_trains(pdf_db, "sin", "KYN", datetime(2026, 9, 28, 5, 0), n=1)
    assert departures.origin == "Sion"
    assert departures.trips[0].number == "96107"  # S 3: CSMT 04:47, Sion 05:11


def test_errors(sample_db, tmp_path):
    with pytest.raises(ScheduleError, match="don't know a station called 'Atlantis'"):
        upcoming_trains(sample_db, "Atlantis", "CSMT", MONDAY_7AM)
    with pytest.raises(ScheduleError, match="same station"):
        upcoming_trains(sample_db, "KYN", "kalyan", MONDAY_7AM)
    empty = tmp_path / "empty.duckdb"
    init_db(empty).close()
    with pytest.raises(ScheduleError, match="isn't loaded"):
        upcoming_trains(empty, "KYN", "CSMT", MONDAY_7AM)


def test_format_departures(sample_db):
    late = datetime(2026, 9, 28, 23, 0)
    departures = upcoming_trains(sample_db, "TNA", "CSMT", late, n=2)
    assert formatting.format_departures(departures, late) == (
        "Next trains Thane → Chhatrapati Shivaji Maharaj Terminus:\n"
        "• Tue 04:44 slow to CSMT, arrives 05:36 (52 min)\n"
        "• Tue 07:32 slow to CSMT, arrives 08:24 (52 min)\n"
        "\n"
        "Timetable times only. Delay and crowd predictions are coming later."
    )
    none = Departures("Kalyan", "Thane", [])
    assert formatting.format_departures(none, late).startswith(
        "No scheduled trains Kalyan → Thane today or tomorrow."
    )


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _run_next(db_path, args):
    message = _Message()
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(args=args, bot_data={handlers.DB_PATH_KEY: db_path})
    asyncio.run(handlers.next_command(update, context))
    return message.replies


def test_next_command(sample_db):
    [reply] = _run_next(sample_db, ["KYN", "CSMT"])
    assert reply.startswith("Next trains Kalyan → Chhatrapati Shivaji Maharaj Terminus:")
    assert reply.endswith("Delay and crowd predictions are coming later.")


def test_next_command_needs_two_stations(sample_db):
    assert _run_next(sample_db, ["KYN"]) == [formatting.NEXT_USAGE]
    [reply] = _run_next(sample_db, ["KYN", "Atlantis"])
    assert reply.startswith("I don't know a station called 'Atlantis'.")


def test_now_is_mumbai_time():
    # The handler passes an aware IST datetime, which next_trains accepts.
    assert datetime.now(IST).utcoffset().total_seconds() == 5.5 * 3600
