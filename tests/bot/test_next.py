"""`/next`: timetable lookups, their formatting and the handler."""

import asyncio
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sitt.bot import formatting, handlers
from sitt.bot.schedule import ScheduleError, recommend_trains, upcoming_trains
from sitt.config import RecommendSettings
from sitt.db import init_db
from sitt.ingest.cr_pdf import SourceInfo, Word, grid_trains, parse_grid, write_csv
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.tz import IST

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


def test_old_sion_code_still_resolves(pdf_db):
    # Sion was SIN in the bot before it took the PDF timetable's code, SION.
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


def test_format_recommendation_without_delay_data(sample_db):
    """With no model and no observations, /next shows timetable times and says so."""
    late = datetime(2026, 9, 28, 23, 0)
    recommendation = recommend_trains(sample_db, "TNA", "CSMT", late)
    assert (recommendation.level, recommendation.synthetic) == ("timetable", False)
    lines = formatting.format_recommendation(recommendation).splitlines()
    assert lines[:5] == [
        "Thane → Chhatrapati Shivaji Maharaj Terminus",
        "",
        "Take the 04:44 slow: arrives 05:36, earliest arrival, likely empty.",
        "",
        "➜ Tue 04:44 slow to CSMT · arr 05:36 · empty",
    ]
    assert lines[5] == "• Tue 07:32 slow to CSMT · arr 08:24 · standing"
    assert lines[-1] == (
        "Timetable times only: there is no delay data yet. "
        "Crowding is a rule-of-thumb estimate. /why for details."
    )


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _context(db_path, args, **bot_data):
    return SimpleNamespace(
        args=args, bot_data={handlers.DB_PATH_KEY: db_path, **bot_data}, user_data={}
    )


def _run_next(db_path, args, **bot_data):
    message = _Message()
    update = SimpleNamespace(effective_message=message)
    asyncio.run(handlers.next_command(update, _context(db_path, args, **bot_data)))
    return message.replies


def test_next_command(sample_db):
    [reply] = _run_next(sample_db, ["KYN", "CSMT"])
    lines = reply.splitlines()
    assert lines[0] == "Kalyan → Chhatrapati Shivaji Maharaj Terminus"
    assert lines[2].startswith(("Take the ", "Wait for the "))
    assert sum(line.startswith("➜ ") for line in lines) == 1
    assert sum(line.startswith(("➜ ", "• ")) for line in lines) == 5
    assert "Timetable times only: there is no delay data yet." in lines[-1]


def test_why_explains_the_last_recommendation(sample_db):
    context = _context(sample_db, ["KYN", "CSMT"])
    first = _Message()
    asyncio.run(handlers.why_command(SimpleNamespace(effective_message=first), context))
    assert first.replies == [formatting.NO_RECOMMENDATION_YET]

    asked = _Message()
    asyncio.run(handlers.next_command(SimpleNamespace(effective_message=asked), context))
    why = _Message()
    asyncio.run(handlers.why_command(SimpleNamespace(effective_message=why), context))
    [text] = why.replies
    assert asked.replies[0].splitlines()[2] in text  # the same one-line recommendation
    assert "Predictions used: timetable only, no delay data." in text
    assert "every train is assumed to run on time" in text
    assert "crowding " in text and "Rules: take the earliest predicted arrival" in text
    assert len(text) < 4096  # fits in one Telegram message


def test_next_uses_the_configured_thresholds(pdf_db, monkeypatch):
    # 08:05 on Monday from Kalyan: the 08:09 ladies' special is listed but not recommended...
    monday = datetime(2026, 9, 28, 8, 5)
    default = recommend_trains(pdf_db, "KYN", "CSMT", monday)
    assert default.chosen.trip.number != "97032"
    assert any(o.trip.number == "97032" for o in default.options)
    # ...unless the settings say the rider can board one and is happy to wait a long time.
    relaxed = RecommendSettings(wait_max_extra_minutes=60, ladies_special_ok=True)
    assert recommend_trains(pdf_db, "KYN", "CSMT", monday, relaxed).chosen.trip.number == "97032"


def test_next_command_needs_two_stations(sample_db):
    assert _run_next(sample_db, ["KYN"]) == [formatting.NEXT_USAGE]
    [reply] = _run_next(sample_db, ["KYN", "Atlantis"])
    assert reply.startswith("I don't know a station called 'Atlantis'.")


def test_now_is_mumbai_time():
    # The handler passes an aware IST datetime, which next_trains accepts.
    assert datetime.now(IST).utcoffset().total_seconds() == 5.5 * 3600


def test_recommendation_lists_mark_ac_15_car_and_ladies_specials(pdf_db):
    # UP page 6 of the PDF: 97032 is a ladies' special and 95712 a 15-car rake.
    morning = datetime(2026, 9, 28, 8, 5)
    settings = RecommendSettings(candidates=6)
    lines = formatting.format_recommendation(
        recommend_trains(pdf_db, "KYN", "CSMT", morning, settings)
    ).splitlines()
    assert "• 08:09 slow to CSMT · arr 09:38 · seats free · LADIES SPECIAL (women only)" in lines
    # Starts at Kalyan (-1) with 15 cars (-0.5): standing rather than packed.
    assert "• 08:33 fast to CSMT · arr 09:37 · standing · 15-car" in lines
    assert "➜ 08:14 fast to CSMT · arr 09:18 · packed" in lines  # nothing to mark

    # DOWN page 1: 95701 (K 3, CSMT 05:20) is an AC local.
    early = datetime(2026, 9, 28, 5, 18)
    trips = upcoming_trains(pdf_db, "CSMT", "KYN", early, n=1).trips
    assert (trips[0].number, trips[0].service_code, trips[0].is_ac) == ("95701", "K 3", True)
    first = formatting.format_recommendation(
        recommend_trains(pdf_db, "CSMT", "KYN", early)
    ).splitlines()[4]
    assert first == "➜ 05:20 fast to Kalyan · arr 06:24 · empty · AC"


def test_trains_without_attributes_show_no_tags(sample_db):
    trip = upcoming_trains(sample_db, "KYN", "CSMT", MONDAY_7AM, n=1).trips[0]
    assert (trip.is_ac, trip.car_count, trip.is_ladies_special) == (None, None, None)
    assert formatting.trip_tags(trip) == []


def test_next_accepts_two_word_station_names(pdf_db):
    [reply] = _run_next(pdf_db, ["Kanjur", "Marg", "Ulhas", "Nagar"])
    assert reply.startswith("Kanjur Marg → Ulhasnagar\n")
    [reply] = _run_next(pdf_db, ["kalwa", "ambarnath"])
    assert reply.startswith("Kalva → Ambernath\n")
    [reply] = _run_next(pdf_db, ["KYN", "Bhivpuri", "Rd"])
    assert reply.startswith("I don't know a station called 'Bhivpuri Rd'.")


class _LogMessage(_Message):
    def __init__(self, text):
        super().__init__()
        self.text = text
        self.chat_id = 1
        self.keyboards = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        self.keyboards.append(reply_markup)
        return SimpleNamespace(message_id=len(self.replies))


def _log_context(db_path, args):
    return SimpleNamespace(
        args=args, bot_data={handlers.DB_PATH_KEY: db_path}, user_data={}, bot=SimpleNamespace()
    )


def _update(message):
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=7))


def test_log_uses_the_stations_table(pdf_db):
    """/log knows stations beyond Kalyan once a timetable is loaded."""
    message = _LogMessage("/log 18:40 fast Ulhas Nagar standing")
    context = _log_context(pdf_db, ["18:40", "fast", "Ulhas", "Nagar", "standing"])
    asyncio.run(handlers.log_command(_update(message), context))
    assert message.replies == ["Logged #1: 18:40 fast from Ulhasnagar (ULNR) · 3/5 standing"]


def test_log_station_buttons_and_typed_station(pdf_db, tmp_path):
    message = _LogMessage("/log 8:12 fast packed")
    context = _log_context(pdf_db, ["8:12", "fast", "packed"])
    asyncio.run(handlers.log_command(_update(message), context))
    buttons = [b.text for row in message.keyboards[0].inline_keyboard for b in row]
    assert buttons[:2] == ["CSMT", "Masjid"]
    assert {"Kasara", "Khopoli", "Kalva"} <= set(buttons)

    # Typing the station instead of tapping one finishes the log.
    async def no_edit(**kwargs):
        return None

    context.bot.edit_message_reply_markup = no_edit
    typed = _LogMessage("titvala")
    asyncio.run(handlers.text_message(_update(typed), context))
    assert typed.replies == ["Logged #1: 08:12 fast from Titwala (TLA) · 4/5 packed"]

    unknown = _LogMessage("Atlantis")
    context.user_data[handlers.DRAFT_KEY] = handlers.LogDraft(raw_text="/log")
    asyncio.run(handlers.text_message(_update(unknown), context))
    assert unknown.replies[0].startswith("I don't know that station.")


def test_log_falls_back_to_the_built_in_list_without_a_timetable(tmp_path):
    empty = tmp_path / "empty.duckdb"
    init_db(empty).close()
    message = _LogMessage("/log 8:12 fast vt packed")
    asyncio.run(
        handlers.log_command(
            _update(message), _log_context(empty, ["8:12", "fast", "vt", "packed"])
        )
    )
    assert message.replies == ["Logged #1: 08:12 fast from CSMT · 4/5 packed"]


def test_ac_weekdays_only_trains_are_not_marked_ac_at_weekends(tmp_path):
    rows = [
        "train_number,destination,service_type,direction,station_code,station_name,"
        "scheduled_arrival,scheduled_departure,days,ac,notes",
        "1,Kalyan,fast,down,TNA,Thane,,08:00,daily,yes,non_ac_weekends",
        "1,Kalyan,fast,down,KYN,Kalyan,08:20,,daily,yes,non_ac_weekends",
        "2,Kalyan,fast,down,TNA,Thane,,08:05,daily,yes,",
        "2,Kalyan,fast,down,KYN,Kalyan,08:25,,daily,yes,",
    ]
    csv_path = tmp_path / "ac.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    path = tmp_path / "ac.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(csv_path))

    friday, saturday = datetime(2026, 10, 2, 7, 0), datetime(2026, 10, 3, 7, 0)
    weekday = upcoming_trains(path, "TNA", "KYN", friday, n=2).trips
    weekend = upcoming_trains(path, "TNA", "KYN", saturday, n=2).trips
    assert [t.runs_ac for t in weekday] == [True, True]
    assert [t.runs_ac for t in weekend] == [False, True]
    assert [formatting.trip_tags(t) for t in weekend] == [[], ["AC"]]
