"""The /log button flow, typed replies, /cancel and /mylogs, with fake Telegram objects."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from sitt.bot import handlers
from sitt.bot.flow import CANCEL_DATA, callback_data
from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable

SAMPLE = Path(__file__).parent.parent / "fixtures" / "sample_timetable.csv"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE))
    return path


class Message:
    def __init__(self, text="", message_id=100):
        self.text = text
        self.chat_id = 1
        self.message_id = message_id
        self.replies: list[str] = []
        self.keyboards: list = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        self.keyboards.append(reply_markup)
        return SimpleNamespace(message_id=self.message_id + len(self.replies))


class Query:
    def __init__(self, data, message_id):
        self.data = data
        self.message = SimpleNamespace(message_id=message_id)
        self.answers: list[str | None] = []
        self.edits: list[str] = []
        self.keyboards: list = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)
        self.keyboards.append(reply_markup)


class Bot:
    def __init__(self):
        self.retired: list[int] = []

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.retired.append(message_id)


def context(db, args=()):
    return SimpleNamespace(
        args=list(args), bot_data={handlers.DB_PATH_KEY: db}, user_data={}, bot=Bot()
    )


def update(message=None, query=None):
    return SimpleNamespace(
        effective_message=message, callback_query=query, effective_user=SimpleNamespace(id=7)
    )


def run(coro):
    return asyncio.run(coro)


def buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def tap(ctx, data):
    draft = ctx.user_data[handlers.DRAFT_KEY]
    query = Query(data, draft.message_id)
    run(handlers.log_callback(update(query=query), ctx))
    return query


def test_log_with_buttons_from_start_to_saved(db):
    ctx = context(db)
    start = Message("/log")
    run(handlers.log_command(update(start), ctx))
    assert callback_data("station", "KYN") in buttons(start.keyboards[0])
    assert CANCEL_DATA in buttons(start.keyboards[0])

    after_station = tap(ctx, callback_data("station", "KYN"))
    assert after_station.edits[-1].startswith("from Kalyan (KYN)")
    time_buttons = [d for d in buttons(after_station.keyboards[-1]) if d.startswith("log:time:")]
    assert time_buttons
    tap(ctx, callback_data("time", "07:12"))
    tap(ctx, callback_data("service", "fast"))
    done = tap(ctx, callback_data("crowd", "4"))
    assert done.edits[-1].startswith("Logged #1: 07:12 fast from Kalyan (KYN) · 4/5 packed")
    assert handlers.DRAFT_KEY not in ctx.user_data

    mine = Message("/mylogs")
    run(handlers.mylogs_command(update(mine), ctx))
    assert mine.replies[0].startswith("Your last 1 report:")
    assert "07:12 fast from KYN · 4/5 packed" in mine.replies[0]


def test_a_double_tap_or_stale_button_does_nothing(db):
    ctx = context(db)
    run(handlers.log_command(update(Message("/log")), ctx))
    tap(ctx, callback_data("station", "KYN"))
    # The same station button again (a double tap): ignored, nothing edited.
    again = tap(ctx, callback_data("station", "KYN"))
    assert again.edits == [] and again.answers == [None]
    # A button from another, older prompt.
    old = Query(callback_data("time", "07:12"), message_id=1)
    run(handlers.log_callback(update(query=old), ctx))
    assert old.answers[0].startswith("That log is finished or expired.")
    assert old.edits == []


def test_cancel_by_button_and_by_command(db):
    ctx = context(db)
    run(handlers.log_command(update(Message("/log")), ctx))
    cancelled = tap(ctx, CANCEL_DATA)
    assert cancelled.edits == ["Cancelled."] and handlers.DRAFT_KEY not in ctx.user_data

    nothing = Message("/cancel")
    run(handlers.cancel_command(update(nothing), ctx))
    assert nothing.replies == ["Nothing to cancel."]

    run(handlers.log_command(update(Message("/log", message_id=200)), ctx))
    prompt_id = ctx.user_data[handlers.DRAFT_KEY].message_id
    by_command = Message("/cancel")
    run(handlers.cancel_command(update(by_command), ctx))
    assert by_command.replies == ["Cancelled."]
    assert ctx.bot.retired == [prompt_id]  # the old buttons were removed


def test_typed_replies_during_a_log(db):
    ctx = context(db, ["KYN", "packed"])
    run(handlers.log_command(update(Message("/log KYN packed")), ctx))
    # Now waiting for the time: a typed time works, nonsense doesn't.
    bad = Message("soon")
    run(handlers.text_message(update(bad), ctx))
    assert bad.replies == ["Couldn't read that as a time. Try something like 8:12 or 20:12."]
    good = Message("7:12")
    run(handlers.text_message(update(good), ctx))
    assert ctx.user_data[handlers.DRAFT_KEY].departure_time.strftime("%H:%M") == "07:12"
    # Now the service: that needs a button.
    typed_service = Message("fast")
    run(handlers.text_message(update(typed_service), ctx))
    assert typed_service.replies == ["Tap one of the buttons above, or /cancel."]


def test_text_with_no_log_in_progress(db):
    message = Message("hello")
    run(handlers.text_message(update(message), context(db)))
    assert message.replies == ["Not sure what to do with that. Send /help to see what I can do."]


def test_conflicting_one_line_log_is_refused(db):
    ctx = context(db, ["8:12", "9:15", "fast", "KYN", "packed"])
    message = Message("/log 8:12 9:15 fast KYN packed")
    run(handlers.log_command(update(message), ctx))
    assert message.replies[0].startswith("Couldn't log that: ")
    assert handlers.DRAFT_KEY not in ctx.user_data


def test_unrecognised_words_are_mentioned(db):
    ctx = context(db, ["8:12", "fast", "KYN", "packed", "banana"])
    message = Message("/log 8:12 fast KYN packed banana")
    run(handlers.log_command(update(message), ctx))
    assert message.replies[0].startswith("Logged #1: 08:12 fast from Kalyan (KYN)")
    assert "(Didn't understand: banana)" in message.replies[0]
