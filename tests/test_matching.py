"""Matching crowd reports to scheduled trains. An invented timetable; no network.

Every train below is made up for these tests. At Kalyan (KYN):

    96001  08:12  fast  up    to CSMT        Mon-Sat
    96002  08:13  fast  down  to Titwala     Mon-Sat   (came from CSMT)
    96003  08:16  fast  up    to CSMT        Mon-Sat
    96005  08:12  slow  up    to CSMT        Mon-Sat
    96021  08:11  slow  down  ends at Kalyan            (can't be boarded there)
    96015  08:14  fast  up    to CSMT        Sundays and holidays only
    96007  08:30  fast  up    AC (not at weekends)      Mon-Sat
    96009  08:30  fast  up    not AC         Mon-Sat
    96011  08:45  fast  up    ladies' special           Mon-Sat
    96013  08:48  fast  up    to CSMT        Mon-Sat
    96023  09:00  fast  up    15 cars        daily
    96025  09:00  fast  up    12 cars        daily
    96017  23:55  slow  up    to CSMT        daily     (reaches Dombivli after midnight)
    96019  00:20  slow  down  to Titwala     Mon-Sat   (left CSMT at 23:20 the day before)
"""

import asyncio
from datetime import date, datetime, time
from types import SimpleNamespace

import duckdb
import pytest

from sitt import matching
from sitt.bot import handlers, matchflow, storage
from sitt.config import MatchSettings, RecommendSettings
from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.matching import ReportQuery
from sitt.models.crowding import CrowdingInput, estimate, similar_reports, train_reports
from sitt.tz import IST

MONDAY, TUESDAY = date(2026, 10, 5), date(2026, 10, 6)
SATURDAY, SUNDAY = date(2026, 10, 10), date(2026, 10, 11)
GANDHI_JAYANTI = date(2026, 10, 2)  # a Friday that runs the Sunday timetable

NAMES = {"CSMT": "CSMT", "DR": "Dadar", "TNA": "Thane", "DI": "Dombivli", "KYN": "Kalyan",
         "TLA": "Titwala"}  # fmt: skip
HEADER = (
    "train_number,destination,service_type,direction,station_code,station_name,"
    "scheduled_arrival,scheduled_departure,days,ac,cars,notes"
)


def up(number, kyn, service="fast", days="mon-sat", ac="no", cars="", notes=""):
    """An up train from Kalyan: Dombivli +7, Thane +21, Dadar +45, CSMT +58 minutes."""
    hours, minutes = map(int, kyn.split(":"))
    start = hours * 60 + minutes

    def clock(offset):
        return f"{(start + offset) // 60 % 24:02d}:{(start + offset) % 60:02d}"

    stops = [("KYN", "", clock(0)), ("DI", clock(7), clock(7)), ("TNA", clock(21), clock(21)),
             ("DR", clock(45), clock(45)), ("CSMT", clock(58), "")]  # fmt: skip
    return [
        f"{number},CSMT,{service},up,{code},{NAMES[code]},{arr},{dep},{days},{ac},{cars},{notes}"
        for code, arr, dep in stops
    ]


def down(number, csmt, kyn, tla=None, service="fast", days="mon-sat"):
    """A down train from CSMT through Thane to Kalyan, and on to Titwala if `tla` is given."""
    label = "Titwala" if tla else "Kalyan"
    hours, minutes = map(int, kyn.split(":"))
    thane = (hours * 60 + minutes - 21) % 1440
    stops = [("CSMT", "", csmt), ("TNA", f"{thane // 60:02d}:{thane % 60:02d}", "")]
    stops.append(("KYN", kyn, kyn if tla else ""))
    if tla:
        stops.append(("TLA", tla, ""))
    rows = []
    for code, arr, dep in stops:
        dep = dep or (arr if code == "TNA" else "")
        rows.append(f"{number},{label},{service},down,{code},{NAMES[code]},{arr},{dep},{days},no,,")
    return rows


TIMETABLE = "\n".join(
    [
        "# INVENTED TEST FIXTURE: not real trains or timings.",
        HEADER,
        *up("96001", "08:12"),
        *down("96002", "07:15", "08:13", "08:25"),
        *up("96003", "08:16"),
        *up("96005", "08:12", service="slow"),
        *down("96021", "07:10", "08:11", service="slow"),
        *up("96015", "08:14", days="sun"),
        *up("96007", "08:30", ac="yes", notes="non_ac_weekends"),
        *up("96009", "08:30"),
        *up("96011", "08:45", notes="ladies_special"),
        *up("96013", "08:48"),
        *up("96023", "09:00", days="daily", cars="15"),
        *up("96025", "09:00", days="daily", cars="12"),
        *up("96017", "23:55", service="slow", days="daily"),
        *down("96019", "23:20", "00:20", "00:35", service="slow"),
    ]
)


@pytest.fixture
def db(tmp_path):
    csv_path = tmp_path / "invented.csv"
    csv_path.write_text(TIMETABLE + "\n", encoding="utf-8")
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(csv_path))
    return path


@pytest.fixture
def con(db):
    with duckdb.connect(str(db)) as con:
        yield con


def at(day: date, hhmm: str) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=IST)


def report(clock: str, logged: datetime, service: str | None = "fast", **extra) -> ReportQuery:
    hours, minutes = map(int, clock.split(":"))
    return ReportQuery("KYN", time(hours, minutes), logged, service, **extra)


def number(result) -> str | None:
    return result.train.number if result.train else None


# --- the matcher ---


def test_an_exact_time_matches_when_its_neighbours_are_further_off(con):
    result = matching.match(con, report("08:16", at(MONDAY, "08:40")))
    # 96003 is exact (1.0); 96002 at 08:13 is three minutes off (0.7): 1.0 - 0.35.
    assert number(result) == "96003" and result.confidence == 0.65
    assert result.train.describe() == "08:16 fast to CSMT"
    assert result.train.departure == datetime(2026, 10, 5, 8, 16)


def test_two_trains_a_minute_apart_are_left_unmatched(con):
    result = matching.match(con, report("08:12", at(MONDAY, "08:40")))
    assert result.train is None and result.reason == "two trains too close to tell apart"
    assert result.confidence == 0.55  # below the 0.6 threshold: better none than a wrong one
    assert [c.number for c in result.candidates][:3] == ["96001", "96002", "96003"]


def test_the_destination_settles_direction(con):
    towards = matching.match(con, report("08:12", at(MONDAY, "08:40"), destination_code="CSMT"))
    assert number(towards) == "96001" and towards.train.direction == "up"
    away = matching.match(con, report("08:12", at(MONDAY, "08:40"), destination_code="TLA"))
    assert number(away) == "96002" and away.train.direction == "down"
    assert away.train.minutes_off == 1 and away.confidence == 0.9


def test_fast_and_slow_at_the_same_minute(con):
    slow = matching.match(con, report("08:12", at(MONDAY, "08:40"), service="slow"))
    assert number(slow) == "96005" and slow.confidence == 1.0
    # Without the type there are three trains within a minute of 08:12.
    assert matching.match(con, report("08:12", at(MONDAY, "08:40"), service=None)).train is None


def test_a_train_that_ends_at_the_station_is_not_a_candidate(con):
    result = matching.match(con, report("08:11", at(MONDAY, "08:40"), service="slow"))
    assert number(result) == "96005"  # not 96021, which terminates at Kalyan at 08:11
    assert "96021" not in [c.number for c in result.candidates]


def test_ac_and_non_ac_trains_at_the_same_time(con):
    logged = at(MONDAY, "09:00")
    unknown = matching.match(con, report("08:30", logged))
    assert unknown.train is None and unknown.confidence == 0.5
    assert {c.number for c in unknown.candidates} >= {"96007", "96009"}
    ac = matching.match(con, report("08:30", logged, is_ac=True))
    assert number(ac) == "96007" and ac.train.describe() == "08:30 fast to CSMT · AC"
    assert number(matching.match(con, report("08:30", logged, is_ac=False))) == "96009"
    # On Saturday the AC rake runs without AC, so "ac" fits nothing and both are non-AC.
    saturday = at(SATURDAY, "09:00")
    assert matching.match(con, report("08:30", saturday, is_ac=True)).candidates == []
    assert matching.match(con, report("08:30", saturday, is_ac=False)).train is None


def test_ladies_specials(con):
    logged = at(MONDAY, "09:15")
    # By default a ladies' special isn't a candidate: the next train, 3 minutes on, is.
    assert number(matching.match(con, report("08:45", logged))) == "96013"
    said = matching.match(con, report("08:45", logged, ladies=True))
    assert number(said) == "96011" and said.confidence == 1.0
    assert said.train.describe() == "08:45 fast to CSMT · ladies special"
    # A rider who can board them gets the exact train, at lower confidence.
    allowed = matching.match(con, report("08:45", logged), ladies_special_ok=True)
    assert number(allowed) == "96011" and allowed.confidence == 0.65


def test_car_count(con):
    logged = at(MONDAY, "09:30")
    assert matching.match(con, report("09:00", logged)).train is None
    long = matching.match(con, report("09:00", logged, car_count=15))
    assert number(long) == "96023" and long.train.describe() == "09:00 fast to CSMT · 15-car"
    assert number(matching.match(con, report("09:00", logged, car_count=12))) == "96025"


def test_holidays_and_sundays_run_the_sunday_timetable(con):
    # On an ordinary Monday the Sunday-only 08:14 doesn't run, and 08:14 sits between trains.
    assert matching.match(con, report("08:14", at(MONDAY, "08:40"))).train is None
    for day in (SUNDAY, GANDHI_JAYANTI):
        result = matching.match(con, report("08:14", at(day, "08:40")))
        assert number(result) == "96015" and result.confidence == 1.0, day
        # The Mon-Sat trains are not candidates at all on such a day.
        assert [c.number for c in result.candidates] == ["96015"]


def test_reports_logged_late(con):
    # That evening, and the next morning before the same time comes round again.
    evening = matching.match(con, report("08:16", at(MONDAY, "21:30")))
    assert evening.train.departure == datetime(2026, 10, 5, 8, 16)
    next_morning = matching.match(con, report("08:16", at(TUESDAY, "07:00")))
    assert next_morning.train.departure == datetime(2026, 10, 5, 8, 16)
    # Logged from the platform, a few minutes before it leaves: today's train.
    early = matching.match(con, report("08:16", at(MONDAY, "08:05")))
    assert early.train.departure == datetime(2026, 10, 5, 8, 16)
    # More than an hour ahead can't be a report yet: it is yesterday's train. Yesterday was
    # a Sunday, when the Mon-Sat 08:16 doesn't run but the Sunday-only 08:14 does.
    yesterday = matching.match(con, report("08:16", at(MONDAY, "06:00")))
    assert number(yesterday) == "96015"
    assert yesterday.train.departure == datetime(2026, 10, 4, 8, 14)
    assert matching.reported_departure(
        report("08:16", at(MONDAY, "06:00")), MatchSettings()
    ) == datetime(2026, 10, 4, 8, 16)


def test_reports_around_midnight(con):
    # The 23:55 from Kalyan, logged ten minutes into the next day.
    late = matching.match(con, report("23:55", at(TUESDAY, "00:05"), service="slow"))
    assert number(late) == "96017" and late.train.departure == datetime(2026, 10, 5, 23, 55)
    # The 00:20 belongs to the run that left CSMT at 23:20 the evening before, so its
    # running days are the evening's. Early on Tuesday that is Monday: it runs.
    tuesday = matching.match(con, report("00:20", at(TUESDAY, "00:40"), service="slow"))
    assert number(tuesday) == "96019"
    assert tuesday.train.departure == datetime(2026, 10, 6, 0, 20)
    # Early on Monday the run would have started on Sunday, and it is a Mon-Sat train.
    assert matching.match(con, report("00:20", at(MONDAY, "00:40"), service="slow")).train is None
    # Logged at 23:50 about the 00:20 still to come: tonight's, not last night's.
    ahead = matching.match(con, report("00:20", at(MONDAY, "23:50"), service="slow"))
    assert ahead.train.departure == datetime(2026, 10, 6, 0, 20)


def test_nothing_near_and_nothing_close_enough(con):
    nothing = matching.match(con, report("14:00", at(MONDAY, "15:00")))
    assert nothing.train is None and nothing.candidates == []
    assert nothing.reason == "no train near that time"
    # 08:58 slow: the nearest slow train is 08:12, far outside every window.
    assert matching.match(con, report("08:58", at(MONDAY, "09:30"), service="slow")).train is None
    # 09:12: the 09:00 pair is 12 minutes off, inside the choice window but scoring nothing.
    far = matching.match(con, report("09:12", at(MONDAY, "09:30")))
    assert far.train is None and far.reason == "no train close enough to that time"
    assert {c.number for c in far.candidates} == {"96023", "96025"}


def test_thresholds_are_configurable(con):
    query = report("08:12", at(MONDAY, "08:40"))
    assert number(matching.match(con, query, MatchSettings(min_confidence=0.5))) == "96001"
    strict = MatchSettings(window_minutes=2)
    assert matching.match(con, report("08:19", at(MONDAY, "08:40")), strict).train is None


# --- stored reports and the backfill command ---


def log(db, text: str, logged: datetime, user: int = 7, level: int = 4) -> int:
    """Store a report the way the bot does, from a quick-log message."""
    with duckdb.connect(str(db)) as con:
        from sitt.bot.flow import LogDraft
        from sitt.bot.parsing import parse_log_text
        from sitt.bot.stations import directory_from

        draft = LogDraft.from_parsed(parse_log_text(text, directory_from(con)), f"/log {text}")
    return storage.insert_report(
        db,
        storage.NewReport(user, logged, draft.station_code, draft.train_description(), level,
                          draft.raw_text),
    )  # fmt: skip


def stored(db, report_id: int) -> tuple:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(
            "SELECT matched_train_id, match_confidence, match_method, matched_at IS NOT NULL "
            "FROM crowd_reports WHERE id = ?",
            [report_id],
        ).fetchone()


def test_extras_in_the_message_are_read_back_for_matching(db):
    report_id = log(db, "8:30 fast KYN packed to CSMT ac", at(MONDAY, "09:00"))
    with duckdb.connect(str(db)) as con:
        query = matching.load_report(con, report_id).query
    assert (query.destination_code, query.is_ac, query.service) == ("CSMT", True, "fast")
    assert query.departure_time == time(8, 30) and query.reported_at == at(MONDAY, "09:00")


def test_backfill_matches_untried_reports_and_respects_human_answers(db):
    clear = log(db, "8:16 fast KYN packed", at(MONDAY, "08:40"))
    unclear = log(db, "8:12 fast KYN standing", at(MONDAY, "08:40"))
    picked = log(db, "8:12 fast KYN standing", at(MONDAY, "08:41"))
    with duckdb.connect(str(db)) as con:
        matching.store_match(con, picked, "central-96002", 1.0, matching.USER)
        con.execute(
            "INSERT INTO crowd_reports (reported_at, train_description, crowd_level, source) "
            "VALUES (?, 'the one after eight', 3, 'telegram:7')",
            [at(MONDAY, "08:40")],
        )
        results = matching.backfill(con)
    assert [(r[0], number(r[2]) if r[2] else "unreadable") for r in results] == [
        (clear, "96003"),
        (unclear, None),
        (4, "unreadable"),
    ]
    assert stored(db, clear) == ("central-96003", 0.65, "backfill", True)
    assert stored(db, unclear) == (None, None, "backfill", True)  # tried, left unmatched
    assert stored(db, picked) == ("central-96002", 1.0, "user", True)  # untouched
    with duckdb.connect(str(db)) as con:
        assert matching.backfill(con) == [(4, "the one after eight", None)]  # nothing left to try
        # --rematch redoes automatic matches (say, after loading a new timetable), not human ones.
        again = matching.backfill(con, MatchSettings(min_confidence=0.5), rematch=True)
    assert {r[0] for r in again} == {clear, unclear, 4}
    assert stored(db, unclear)[0] == "central-96001"
    assert stored(db, picked) == ("central-96002", 1.0, "user", True)


def test_cli_dry_run_then_real(db, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    report_id = log(db, "8:16 fast KYN packed", at(MONDAY, "08:40"))
    log(db, "8:12 fast KYN packed", at(MONDAY, "08:40"))

    assert matching.main(["--db", str(db), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "#1 08:16 fast from KYN: 08:16 fast to CSMT [central-96003, confidence 0.65]" in out
    assert "#2 08:12 fast from KYN: left unmatched (two trains too close to tell apart)" in out
    assert "1 of 2 report(s) matched (dry run, nothing stored)." in out
    assert stored(db, report_id) == (None, None, None, False)

    assert matching.main(["--db", str(db)]) == 0
    assert stored(db, report_id)[:3] == ("central-96003", 0.65, "backfill")
    assert matching.main(["--db", str(tmp_path / "nope.duckdb"), "--dry-run"]) == 2
    assert "No database at" in capsys.readouterr().err


# --- the bot: confirmation line and the correction buttons ---


class Message:
    def __init__(self, text):
        self.text, self.chat_id = text, 1
        self.replies, self.keyboards = [], []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        self.keyboards.append(reply_markup)
        return SimpleNamespace(message_id=len(self.replies))


class Query:
    """A tap on an inline button under `text`."""

    def __init__(self, data, text):
        self.data = data
        self.message = SimpleNamespace(text=text, message_id=1)
        self.answers, self.edits = [], []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


def context(db, args=(), ladies_ok=False):
    return SimpleNamespace(
        args=list(args),
        bot_data={
            handlers.DB_PATH_KEY: db,
            handlers.MATCH_SETTINGS_KEY: MatchSettings(),
            handlers.RECOMMEND_SETTINGS_KEY: RecommendSettings(ladies_special_ok=ladies_ok),
        },
        user_data={},
        bot=SimpleNamespace(),
    )


def update(message=None, query=None, user=7):
    return SimpleNamespace(
        effective_message=message, callback_query=query, effective_user=SimpleNamespace(id=user)
    )


def labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def tap(db, data, text, user=7) -> Query:
    query = Query(data, text)
    asyncio.run(handlers.match_callback(update(query=query, user=user), context(db)))
    return query


@pytest.fixture
def monday_morning(monkeypatch):
    """Freeze the bot's clock at 08:40 on Monday 5 October 2026."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at(MONDAY, "08:40").astimezone(tz) if tz else datetime(2026, 10, 5, 8, 40)

    monkeypatch.setattr(handlers, "datetime", Clock)


def test_log_confirms_the_matched_train_with_a_way_to_correct_it(db, monday_morning):
    message = Message("/log 8:16 fast KYN packed")
    asyncio.run(
        handlers.log_command(update(message), context(db, ["8:16", "fast", "KYN", "packed"]))
    )
    assert message.replies == [
        "Logged #1: 08:16 fast from Kalyan (KYN) · 4/5 packed\nMatched to the 08:16 fast to CSMT."
    ]
    assert labels(message.keyboards[0]) == ["Not that train"]
    assert stored(db, 1) == ("central-96003", 0.65, "auto", True)

    # "Not that train" lists the trains around that time, in time order.
    picking = tap(db, "match:1:pick", message.replies[0])
    text, markup = picking.edits[0]
    assert text == "Logged #1: 08:16 fast from Kalyan (KYN) · 4/5 packed\nWhich train was it?"
    assert labels(markup) == [
        "08:12 fast to CSMT",
        "08:13 fast to Titwala",
        "08:16 fast to CSMT",
        "08:30 fast to CSMT · AC",
        "None of these",
    ]

    chosen = tap(db, "match:1:t:central-96002", text)
    assert chosen.edits[0][0] == (
        "Logged #1: 08:16 fast from Kalyan (KYN) · 4/5 packed\n"
        "Train: 08:13 fast to Titwala (your pick)."
    )
    assert chosen.edits[0][1] is None  # the buttons are gone
    assert stored(db, 1) == ("central-96002", 1.0, "user", True)


def test_an_unclear_log_offers_the_candidates_straight_away(db, monday_morning):
    message = Message("/log 8:12 fast KYN standing")
    asyncio.run(
        handlers.log_command(update(message), context(db, ["8:12", "fast", "KYN", "standing"]))
    )
    assert message.replies[0].endswith("I couldn't tell which train that was. Tap it if you know:")
    assert labels(message.keyboards[0]) == [
        "08:12 fast to CSMT",
        "08:13 fast to Titwala",
        "08:16 fast to CSMT",
        "08:30 fast to CSMT · AC",
        "None of these",
    ]
    assert stored(db, 1) == (None, None, "auto", True)

    none = tap(db, "match:1:none", message.replies[0])
    assert none.edits[0][0].endswith("\nNot matched to a train.")
    assert stored(db, 1) == (None, None, "user_none", True)
    with duckdb.connect(str(db)) as con:
        assert matching.backfill(con, rematch=True) == []  # the rider's "none" stands


def test_a_log_with_no_train_nearby_is_just_logged(db, monday_morning):
    message = Message("/log 14:00 fast KYN empty")
    asyncio.run(
        handlers.log_command(update(message), context(db, ["14:00", "fast", "KYN", "empty"]))
    )
    assert message.replies == ["Logged #1: 14:00 fast from Kalyan (KYN) · 1/5 empty"]
    assert message.keyboards == [None]


def test_a_matching_failure_never_loses_the_log(db, monday_morning, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(matchflow, "match_report", broken)
    message = Message("/log 8:16 fast KYN packed")
    asyncio.run(
        handlers.log_command(update(message), context(db, ["8:16", "fast", "KYN", "packed"]))
    )
    assert message.replies == ["Logged #1: 08:16 fast from Kalyan (KYN) · 4/5 packed"]
    assert stored(db, 1) == (None, None, None, False)


def test_buttons_only_work_for_the_owner_and_for_nearby_trains(db, monday_morning):
    report_id = log(db, "8:16 fast KYN packed", at(MONDAY, "08:40"), user=7)
    text = "Logged #1: 08:16 fast from Kalyan (KYN) · 4/5 packed"

    stranger = tap(db, f"match:{report_id}:t:central-96003", text, user=99)
    assert stranger.answers == ["That isn't one of your reports."] and stranger.edits == []
    forged = tap(db, f"match:{report_id}:t:central-96017", text)  # the 23:55: nowhere near
    assert forged.answers == ["That train isn't near the time you logged."]
    missing = tap(db, "match:999:pick", text)
    assert missing.answers == ["That isn't one of your reports."]
    garbage = tap(db, "match:abc", text)
    assert garbage.answers == [None] and garbage.edits == []
    assert stored(db, report_id) == (None, None, None, False)


def test_callback_data_round_trips_and_fits_telegrams_limit():
    data = matchflow.callback_data(123456, matchflow.TRAIN, "central-96301-sun")
    assert matchflow.parse_callback_data(data) == (123456, "t", "central-96301-sun")
    assert len(data.encode()) <= 64
    assert matchflow.parse_callback_data("match:5:pick") == (5, "pick", None)
    for bad in ("match:5", "match:x:pick", "log:5:pick", "match:5:t", "match:5:pick:extra"):
        with pytest.raises(ValueError):
            matchflow.parse_callback_data(bad)


# --- crowding uses a train's own reports ---


def test_train_reports_and_similar_reports_do_not_overlap(db):
    own = log(db, "8:16 fast KYN packed", at(MONDAY, "08:40"), level=5)
    other = log(db, "8:30 fast KYN seats ac", at(MONDAY, "09:00"), level=2)
    log(db, "8:12 fast KYN standing", at(MONDAY, "08:40"), level=3)  # stays unmatched
    with duckdb.connect(str(db)) as con:
        matching.backfill(con)
        now = datetime(2026, 10, 6, 8, 0)
        departure = datetime(2026, 10, 6, 8, 16)
        assert train_reports(con, "central-96003", "KYN", now) == [5]
        assert train_reports(con, "central-96003", "DI", now) == []  # boarded elsewhere
        assert train_reports(con, "central-96003", "KYN", datetime(2027, 6, 1)) == []  # too old
        everything = similar_reports(con, "KYN", "fast", departure, now)
        assert sorted(everything) == [2, 3, 5]  # the old behaviour, unchanged
        without_own = similar_reports(
            con, "KYN", "fast", departure, now, exclude_train_id="central-96003"
        )
        assert sorted(without_own) == [2, 3]
    assert stored(db, own)[0] == "central-96003" and stored(db, other)[0] == "central-96007"


def test_a_trains_own_reports_count_for_more_than_similar_ones():
    off_peak = CrowdingInput("up", "fast", datetime(2026, 10, 6, 13, 0))  # rule score 2
    assert estimate(off_peak).score == 2
    # One report of 5 for a similar train: a quarter of the way, 2.75, rounds to 3.
    similar_only = estimate(off_peak, [5])
    assert (similar_only.score, similar_only.train_reports_used) == (3, 0)
    assert similar_only.reason.endswith("your 1 report average 5.0")
    # The same report for this exact train moves it just as far...
    own = estimate(off_peak, train_reports=[5])
    assert (own.score, own.reports_used, own.train_reports_used) == (3, 1, 1)
    assert own.reason.endswith("your 1 report for this train average 5.0")
    # ...and when both exist, the neighbours' reports count half.
    both = estimate(off_peak, [1, 1], train_reports=[5, 5])
    assert both.reports_mean == pytest.approx((5 + 5 + 0.5 + 0.5) / 3)
    assert both.reason.endswith(
        "your 2 reports for this train and 2 for similar trains average 3.7"
    )
    assert both.score == 3  # half rules (2), half reports (3.67)
    assert estimate(off_peak, [1, 1, 5, 5]).score == 3  # all similar: mean 3, weight 4/7
