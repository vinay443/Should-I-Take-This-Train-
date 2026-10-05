"""Saved routes, /commute, the morning message and the crowd prompt. No Telegram calls.

Uses the invented sample timetable. From Kalyan the 90106 fast leaves at 07:12 and
reaches CSMT at 08:19. One more invented train, 90190, leaves CSMT at 23:40 and reaches
Kalyan at 00:55, to exercise midnight.
"""

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from sitt.bot import commute, favourites, handlers
from sitt.bot.app import COMMUTE_JOB_NAME, build_application, schedule_commute_messages
from sitt.bot.favourites import Favourite, FavouriteError
from sitt.bot.stations import load_directory
from sitt.config import CommuteSettings, Settings
from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.tz import IST

FIXTURES = Path(__file__).parent.parent / "fixtures"
ME, STRANGER = 7, 99
MONDAY, FRIDAY, SATURDAY, SUNDAY = (date(2026, 10, d) for d in (5, 9, 10, 11))
GANDHI_JAYANTI = date(2026, 10, 2)  # a Friday holiday
SETTINGS = CommuteSettings()

LATE_TRAIN = """\
# INVENTED TEST FIXTURE: not a real train.
train_number,destination,service_type,direction,station_code,station_name,scheduled_arrival,scheduled_departure,days
90190,Kalyan,fast,down,CSMT,Chhatrapati Shivaji Maharaj Terminus,,23:40,daily
90190,Kalyan,fast,down,DR,Dadar,23:57,23:58,daily
90190,Kalyan,fast,down,TNA,Thane,00:30,00:31,daily
90190,Kalyan,fast,down,KYN,Kalyan,00:55,,daily
"""


@pytest.fixture
def db(tmp_path):
    late = tmp_path / "late.csv"
    late.write_text(LATE_TRAIN, encoding="utf-8")
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(FIXTURES / "sample_timetable.csv"))
        load_timetable(con, read_timetable(late))
    return path


@pytest.fixture
def stations(db):
    return load_directory(db)


def at(day: date, hhmm: str) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes)


def save(db, name="work", origin="KYN", destination="CSMT", usual="07:12", days="YYYYYNN", user=ME):
    usual_time = time(*map(int, usual.split(":"))) if usual else None
    return favourites.add_favourite(
        db, Favourite(user, name, origin, destination, usual_time, days)
    )


def kinds(db, now, settings=SETTINGS, allowed=frozenset({ME})):
    return [
        (item.kind, item.favourite.name, item.day)
        for item in commute.check_due(db, now, settings, allowed)
    ]


# --- /fav parsing and storage ---


def test_parse_add(stations):
    parsed = favourites.parse_add(ME, ["Work", "KYN", "CSMT", "7:12", "mon-fri"], stations)
    assert parsed == Favourite(ME, "work", "KYN", "CSMT", time(7, 12), "YYYYYNN")
    # Days and time in the other order, a two-word station, and an alias.
    other = favourites.parse_add(
        ME, ["home", "vt", "Kanjur", "Marg", "mon-sat", "6:40pm"], stations
    )
    assert (other.from_station, other.to_station) == ("CSMT", "KJRD")
    assert (other.usual_departure, other.weekdays) == (time(18, 40), "YYYYYYN")
    plain = favourites.parse_add(ME, ["gym", "thane", "dadar"], stations)
    assert (plain.usual_departure, plain.weekdays) == (None, "YYYYYNN")
    assert favourites.parse_add(ME, ["x", "KYN", "DR", "daily"], stations).weekdays == "YYYYYYY"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["work"], "Save a route like this"),
        (["work", "KYN"], "Save a route like this"),
        (["my route!", "KYN", "CSMT"], "can't be a name"),
        (["work", "KYN", "Atlantis"], "I don't know a station called 'Atlantis'"),
        (["work", "KYN", "kalyan"], "Those are the same station."),
        (["work", "KYN", "CSMT", "mon-funday"], "couldn't read 'mon-funday' as days"),
    ],
)
def test_parse_add_says_what_is_wrong(stations, args, message):
    with pytest.raises(FavouriteError, match=message):
        favourites.parse_add(ME, args, stations)


def test_favourites_are_stored_per_user_with_one_default(db, stations):
    first = save(db, "work")
    second = save(db, "home", "CSMT", "KYN", "18:05")
    save(db, "theirs", user=STRANGER)
    assert first.is_default and not second.is_default
    assert [f.name for f in favourites.list_favourites(db, ME)] == ["work", "home"]
    assert len(favourites.list_favourites(db)) == 3

    assert favourites.set_default(db, ME, "home") and not favourites.set_default(db, ME, "nope")
    assert [f.name for f in favourites.list_favourites(db, ME)] == ["home", "work"]
    # Saving a name again replaces the route but keeps its default and its switches.
    assert favourites.set_flag(db, ME, "home", "nudge", False)
    again = save(db, "home", "DR", "KYN", "18:20")
    assert (again.from_station, again.is_default, again.nudge, again.notify) == (
        "DR", True, False, True,
    )  # fmt: skip
    # Removing the default hands it to another.
    assert favourites.remove_favourite(db, ME, "HOME")
    assert not favourites.remove_favourite(db, ME, "home")
    assert [(f.name, f.is_default) for f in favourites.list_favourites(db, ME)] == [("work", True)]
    assert favourites.list_favourites(db, STRANGER)[0].is_default
    with pytest.raises(ValueError):
        favourites.set_flag(db, ME, "work", "is_default", True)


def test_describe(stations):
    favourite = Favourite(ME, "work", "KYN", "DR", time(7, 12), "YYYYYNN", True, True, False)
    assert favourites.describe(favourite, stations) == (
        "work: Kalyan (KYN) → Dadar (DR), usual train 07:12, mon-fri (nudge off) [default]"
    )
    assert favourites.describe(Favourite(ME, "gym", "TNA", "DR"), stations) == (
        "gym: Thane (TNA) → Dadar (DR)"
    )
    assert favourites.describe_days("YYYYYYY") == "daily"
    assert favourites.describe_days("YNYNYNN") == "mon, wed, fri"
    assert favourites.describe_days("NNNNNYY") == "sat, sun"


# --- which route /commute uses ---


def test_commute_uses_the_default_and_the_return_leg_in_the_afternoon():
    work = Favourite(ME, "work", "KYN", "CSMT", time(7, 12), is_default=True)
    home = Favourite(ME, "home", "CSMT", "KYN", time(18, 5))
    gym = Favourite(ME, "gym", "TNA", "DR")
    assert favourites.pick_for_commute([], at(MONDAY, "08:00")) is None
    assert favourites.pick_for_commute([work, gym], at(MONDAY, "18:00")) == work  # no pair
    pair = [work, home, gym]
    assert favourites.pick_for_commute(pair, at(MONDAY, "07:00")) == work
    assert favourites.pick_for_commute(pair, at(MONDAY, "13:59")) == work
    assert favourites.pick_for_commute(pair, at(MONDAY, "14:00")) == home
    late = CommuteSettings(return_after=time(16, 30))
    assert favourites.pick_for_commute(pair, at(MONDAY, "15:00"), late) == work
    # Whichever of the pair is the default, the earlier usual train is the outbound leg.
    swapped = [Favourite(ME, "work", "KYN", "CSMT", time(7, 12)),
               Favourite(ME, "home", "CSMT", "KYN", time(18, 5), is_default=True)]  # fmt: skip
    assert favourites.pick_for_commute(swapped, at(MONDAY, "07:00")).name == "work"
    assert favourites.pick_for_commute(swapped, at(MONDAY, "19:00")).name == "home"


# --- what is due, and when ---


def test_the_morning_message_is_due_twenty_minutes_before_the_usual_train(db):
    save(db)
    assert kinds(db, at(MONDAY, "06:51")) == []
    assert kinds(db, at(MONDAY, "06:52")) == [("notify", "work", MONDAY)]
    assert kinds(db, at(MONDAY, "07:01")) == [("notify", "work", MONDAY)]  # within the grace
    assert kinds(db, at(MONDAY, "07:02")) == []  # too late to be useful
    early = CommuteSettings(notify_lead_minutes=45)
    assert kinds(db, at(MONDAY, "06:27"), early) == [("notify", "work", MONDAY)]


def test_each_message_goes_out_once_a_day(db):
    save(db)
    (item,) = commute.check_due(db, at(MONDAY, "06:52"), SETTINGS, frozenset({ME}))
    commute.record_sent(db, item)
    assert kinds(db, at(MONDAY, "06:53")) == []
    assert kinds(db, at(date(2026, 10, 6), "06:52")) == [("notify", "work", date(2026, 10, 6))]


def test_nothing_is_due_on_other_days_holidays_or_for_other_people(db):
    save(db)
    assert kinds(db, at(SATURDAY, "06:52")) == []  # mon-fri
    assert kinds(db, at(GANDHI_JAYANTI, "06:52")) == []  # a Friday, but a holiday
    working_holidays = CommuteSettings(skip_sunday_schedule=False)
    assert kinds(db, at(GANDHI_JAYANTI, "06:52"), working_holidays) == [
        ("notify", "work", GANDHI_JAYANTI)
    ]
    save(db, "sunday", days="NNNNNNY")
    assert kinds(db, at(SUNDAY, "06:52")) == []  # Sundays are skipped by default too
    assert [k[1] for k in kinds(db, at(SUNDAY, "06:52"), working_holidays)] == ["sunday"]

    assert kinds(db, at(MONDAY, "06:52"), allowed=frozenset({STRANGER})) == []
    save(db, "theirs", user=STRANGER)
    assert [k[1] for k in kinds(db, at(MONDAY, "06:52"))] == ["work"]


def test_switches(db):
    save(db)
    save(db, "untimed", "TNA", "DR", usual=None)
    assert kinds(db, at(MONDAY, "06:52"), CommuteSettings(notify_enabled=False)) == []
    assert kinds(db, at(MONDAY, "08:24"), CommuteSettings(nudge_enabled=False)) == []
    favourites.set_flag(db, ME, "work", "notify", False)
    favourites.set_flag(db, ME, "work", "nudge", False)
    assert kinds(db, at(MONDAY, "06:52")) == [] and kinds(db, at(MONDAY, "08:24")) == []


def test_the_crowd_prompt_is_due_after_the_usual_train_arrives(db):
    save(db)
    assert kinds(db, at(MONDAY, "08:23")) == []
    (item,) = commute.check_due(db, at(MONDAY, "08:24"), SETTINGS, frozenset({ME}))
    assert (item.kind, item.day) == ("nudge", MONDAY)
    assert (item.trip.number, item.trip.train_id) == ("90106", "central-90106")
    assert item.trip.arrival == at(MONDAY, "08:19")
    assert commute.train_description(item.trip, "KYN") == "07:12 fast from KYN"
    # A usual time a few minutes off still finds the train; one nowhere near finds none.
    save(db, "roughly", usual="07:08")
    assert ("nudge", "roughly", MONDAY) in kinds(db, at(MONDAY, "08:24"))
    save(db, "nothing", usual="14:00")
    assert all(
        name != "nothing" for kind, name, _ in kinds(db, at(MONDAY, "15:10")) if kind == "nudge"
    )
    with duckdb.connect(str(db)) as con:
        nothing = next(f for f in favourites.read_favourites(con, ME) if f.name == "nothing")
        assert commute.usual_trip(con, nothing, MONDAY, SETTINGS) is None


def test_a_train_that_runs_past_midnight(db):
    save(db, "late", "CSMT", "KYN", "23:40")
    assert kinds(db, at(FRIDAY, "23:20")) == [("notify", "late", FRIDAY)]
    # It arrives at 00:55 on Saturday. The prompt belongs to Friday's run, so Saturday not
    # being a travel day doesn't stop it.
    assert kinds(db, at(SATURDAY, "00:59")) == []
    assert kinds(db, at(SATURDAY, "01:00")) == [("nudge", "late", FRIDAY)]
    assert kinds(db, at(SUNDAY, "01:00")) == []  # nothing left on Saturday night
    # Monday's reminder for a 00:10 train falls on Sunday evening; Saturday's isn't sent.
    save(db, "owl", "KYN", "CSMT", "00:10")
    assert kinds(db, at(SUNDAY, "23:50")) == [("notify", "owl", date(2026, 10, 12))]
    assert kinds(db, at(FRIDAY, "23:50")) == []


# --- handlers ---


class Message:
    def __init__(self, text=""):
        self.text, self.chat_id = text, 1
        self.replies, self.keyboards = [], []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        self.keyboards.append(reply_markup)
        return SimpleNamespace(message_id=len(self.replies))


class Bot:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append((chat_id, text, reply_markup))


class Query:
    def __init__(self, data):
        self.data, self.message = data, SimpleNamespace(text="", message_id=1)
        self.answers, self.edits = [], []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)


def context(db, args=(), bot=None, allowed=frozenset({ME}), settings=SETTINGS):
    remembered: dict[int, dict] = {}

    class UserData(dict):
        def __missing__(self, key):
            return remembered.setdefault(key, {})

    return SimpleNamespace(
        args=list(args),
        bot_data={
            handlers.DB_PATH_KEY: db,
            handlers.COMMUTE_SETTINGS_KEY: settings,
            handlers.ALLOWED_USERS_KEY: allowed,
            handlers.MODEL_DIR_KEY: None,
        },
        user_data={},
        bot=bot or Bot(),
        application=SimpleNamespace(user_data=UserData()),
    )


def update(message=None, query=None, user=ME):
    return SimpleNamespace(
        effective_message=message, callback_query=query, effective_user=SimpleNamespace(id=user)
    )


def fav(db, *args, user=ME) -> str:
    message = Message()
    asyncio.run(handlers.fav_command(update(message, user=user), context(db, args)))
    assert len(message.replies) == 1
    return message.replies[0]


@pytest.fixture
def clock(monkeypatch):
    """Set the bot's idea of now: `clock(MONDAY, "06:52")`."""

    def set_time(day: date, hhmm: str) -> None:
        moment = at(day, hhmm).replace(tzinfo=IST)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        monkeypatch.setattr(handlers, "datetime", Clock)

    set_time(MONDAY, "06:00")
    return set_time


def labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_fav_command_round_trip(db, clock):
    assert fav(db).startswith("No saved routes yet.\nSave a route like this:")
    saved = fav(db, "add", "work", "KYN", "CSMT", "7:12", "mon-fri")
    assert saved.splitlines() == [
        "Saved work: Kalyan (KYN) → Chhatrapati Shivaji Maharaj Terminus (CSMT), usual train "
        "07:12, mon-fri [default]",
        "On those days I'll send the recommendation 20 min before. /fav notify work off "
        "stops that.",
        "After the 07:12 fast to CSMT arrives I'll ask how crowded it was. /fav nudge work off "
        "stops that.",
        "Both only happen while this bot is running.",
    ]
    assert "No usual train time" in fav(db, "add", "gym", "thane", "dadar")
    assert "has no train within 10 min of 14:00" in fav(db, "add", "odd", "KYN", "CSMT", "14:00")
    listing = fav(db, "list")
    assert listing.startswith("Your saved routes:\n• work: Kalyan (KYN)")
    assert "• gym: Thane (TNA) → Dadar (DR)" in listing

    assert fav(db, "nudge", "work", "off") == "The crowding question is off for work."
    assert fav(db, "notify", "work", "off") == "The message before your train is off for work."
    assert "(notify and nudge off)" in fav(db)
    assert fav(db, "default", "gym") == "/commute now uses gym."
    assert fav(db, "remove", "gym") == "Removed gym."
    assert fav(db, "remove", "gym") == "You have no saved route called 'gym'. /fav list shows them."
    assert fav(db, "add", "work", "KYN", "Atlantis") == "I don't know a station called 'Atlantis'."
    assert fav(db, "nudge", "work", "maybe").startswith("/fav add <name> <from> <to>")
    assert fav(db, "list", user=STRANGER).startswith("No saved routes yet.")  # per user


def test_commute_command(db, clock):
    message = Message()
    asyncio.run(handlers.commute_command(update(message), context(db)))
    assert message.replies[0].startswith("No saved routes yet.")

    save(db, "work")
    save(db, "home", "CSMT", "KYN", "18:05")
    clock(MONDAY, "07:00")
    morning, ctx = Message(), context(db)
    asyncio.run(handlers.commute_command(update(morning), ctx))
    lines = morning.replies[0].splitlines()
    assert lines[:2] == ["work", "Kalyan → Chhatrapati Shivaji Maharaj Terminus"]
    assert any("07:12 fast to CSMT" in line for line in lines)
    assert ctx.user_data[handlers.LAST_RECOMMENDATION_KEY].origin == "Kalyan"  # so /why works

    clock(MONDAY, "17:30")
    evening = Message()
    asyncio.run(handlers.commute_command(update(evening), context(db)))
    assert evening.replies[0].splitlines()[:2] == [
        "home",
        "Chhatrapati Shivaji Maharaj Terminus → Kalyan",
    ]


def test_commute_command_survives_a_route_the_timetable_no_longer_has(db, clock):
    save(db, "old", "KYN", "CSMT")
    with duckdb.connect(str(db)) as con:
        con.execute("UPDATE favourite_routes SET to_station = 'ZZZ'")
    message = Message()
    asyncio.run(handlers.commute_command(update(message), context(db)))
    assert "I don't know a station called 'ZZZ'." in message.replies[0]
    assert "may need saving again with /fav add" in message.replies[0]


def test_tick_sends_the_morning_message_once(db, clock):
    save(db)
    save(db, "theirs", user=STRANGER)  # not on the allow-list: never messaged
    bot = Bot()
    ctx = context(db, bot=bot)
    clock(MONDAY, "06:40")
    asyncio.run(handlers.commute_tick(ctx))
    assert bot.sent == []

    clock(MONDAY, "06:52")
    asyncio.run(handlers.commute_tick(ctx))
    asyncio.run(handlers.commute_tick(ctx))  # the next minute's tick, same window
    assert len(bot.sent) == 1
    chat_id, text, markup = bot.sent[0]
    assert chat_id == ME and markup is None
    assert text.splitlines()[:2] == [
        "Your work commute",
        "Kalyan → Chhatrapati Shivaji Maharaj Terminus",
    ]
    assert ctx.application.user_data[ME][handlers.LAST_RECOMMENDATION_KEY] is not None


def test_tick_sends_the_crowd_prompt_and_one_tap_logs_a_matched_report(db, clock):
    save(db)
    bot = Bot()
    clock(MONDAY, "08:24")
    asyncio.run(handlers.commute_tick(context(db, bot=bot)))
    ((chat_id, text, markup),) = bot.sent
    assert chat_id == ME
    assert text == (
        "How crowded was the 07:12 fast to CSMT from Kalyan (KYN)?\n"
        "1 empty · 2 seats free · 3 standing · 4 packed · 5 can't board"
    )
    assert labels(markup) == ["1", "2", "3", "4", "5", "Didn't take it", "Stop asking"]
    data = markup.inline_keyboard[0][3].callback_data
    assert data == "nudge:20261005:4:work" and len(data.encode()) <= 64

    tap = Query(data)
    asyncio.run(handlers.nudge_callback(update(query=tap), context(db)))
    assert tap.edits == [
        "Logged #1: 07:12 fast from KYN · 4/5 packed, matched to that train. Thanks."
    ]
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute(
            "SELECT station_code, train_description, crowd_level, source, note, "
            "matched_train_id, match_confidence, match_method FROM crowd_reports"
        ).fetchall() == [
            ("KYN", "07:12 fast from KYN", 4, "telegram:7", "nudge: work", "central-90106", 1.0,
             "nudge")
        ]  # fmt: skip

    again = Query(data)
    asyncio.run(handlers.nudge_callback(update(query=again), context(db)))
    assert again.answers == ["Already answered."] and again.edits == []
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM crowd_reports").fetchone() == (1,)


def test_the_prompt_can_be_skipped_or_switched_off(db, clock):
    save(db)
    clock(MONDAY, "08:24")
    asyncio.run(handlers.commute_tick(context(db)))
    skipped = Query("nudge:20261005:skip:work")
    asyncio.run(handlers.nudge_callback(update(query=skipped), context(db)))
    assert skipped.edits == ["OK, nothing logged for the 07:12 fast from KYN."]

    clock(date(2026, 10, 6), "08:24")
    asyncio.run(handlers.commute_tick(context(db)))
    off = Query("nudge:20261006:off:work")
    asyncio.run(handlers.nudge_callback(update(query=off), context(db)))
    assert off.edits == [
        "I won't ask about your work commute again. /fav nudge work on turns it back on."
    ]
    assert not favourites.list_favourites(db, ME)[0].nudge
    assert kinds(db, at(date(2026, 10, 7), "08:24")) == []
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM crowd_reports").fetchone() == (0,)


def test_stale_forged_or_foreign_taps_do_nothing(db, clock):
    save(db)
    clock(MONDAY, "08:24")
    asyncio.run(handlers.commute_tick(context(db)))
    for data, user in (
        ("nudge:20261005:4:work", STRANGER),  # someone else's prompt
        ("nudge:20260101:4:work", ME),  # a day no prompt was sent
        ("nudge:20261005:4:elsewhere", ME),  # no such route
    ):
        tap = Query(data)
        asyncio.run(handlers.nudge_callback(update(query=tap, user=user), context(db)))
        assert tap.answers == ["That question has expired."] and tap.edits == []
    for bad in ("nudge:20261005:9:work", "nudge:x", "nudge:20261005:4:"):
        tap = Query(bad)
        asyncio.run(handlers.nudge_callback(update(query=tap), context(db)))
        assert tap.answers == [None] and tap.edits == []
        with pytest.raises(ValueError):
            commute.parse_callback_data(bad)
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM crowd_reports").fetchone() == (0,)


def test_tick_survives_a_busy_database_and_a_failed_send(db, clock, monkeypatch):
    save(db)
    clock(MONDAY, "06:52")

    def busy(*args, **kwargs):
        raise duckdb.IOException("file is locked")

    monkeypatch.setattr(commute, "check_due", busy)
    asyncio.run(handlers.commute_tick(context(db)))  # logs a warning, raises nothing
    monkeypatch.undo()
    clock(MONDAY, "06:52")

    failing = Bot(fail=True)
    asyncio.run(handlers.commute_tick(context(db, bot=failing)))
    # Recorded before sending, so a failed send is not retried every minute.
    assert kinds(db, at(MONDAY, "06:53")) == []


def test_the_job_is_scheduled_unless_both_messages_are_off():
    def settings(**commute_settings) -> Settings:
        return Settings(
            db_path=Path("unused.duckdb"),
            log_level="INFO",
            telegram_bot_token="123456:TEST-TOKEN",
            allowed_user_ids=frozenset({ME}),
            commute=CommuteSettings(**commute_settings),
        )

    application = build_application(settings())
    assert [job.name for job in application.job_queue.jobs()] == [COMMUTE_JOB_NAME]
    assert application.bot_data[handlers.ALLOWED_USERS_KEY] == frozenset({ME})

    off = settings(notify_enabled=False, nudge_enabled=False)
    assert build_application(off).job_queue.jobs() == ()
    without_queue = SimpleNamespace(job_queue=None)
    assert schedule_commute_messages(without_queue, settings()) is False
