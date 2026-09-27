"""Telegram handlers. Kept thin: parsing and flow logic live in plain modules."""

import contextlib
import logging
from datetime import UTC, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from sitt.bot import formatting, storage
from sitt.bot.flow import (
    CANCEL_DATA,
    IST,
    LogDraft,
    Step,
    callback_data,
    parse_callback_data,
    time_choices,
)
from sitt.bot.parsing import CROWD_LEVELS, SERVICES, parse_log_text, parse_time
from sitt.bot.stations import STATIONS

logger = logging.getLogger(__name__)

DRAFT_KEY = "log_draft"
DB_PATH_KEY = "db_path"

_PROMPTS: dict[Step, str] = {
    "station": "Where did you board?",
    "time": "Roughly when did it leave? Tap a time or type one (e.g. 8:12).",
    "service": "Fast or slow?",
    "crowd": "How crowded was it?",
}


def make_access_gate(allowed_user_ids: frozenset[int]):
    """Build a group -1 handler that stops updates from anyone not allowed."""

    async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is not None and user.id in allowed_user_ids:
            return
        logger.warning("Ignoring update from unauthorised user %s", user.id if user else None)
        chat = update.effective_chat
        if (
            user is not None
            and chat is not None
            and chat.type == ChatType.PRIVATE
            and update.message
        ):
            # Replying with the ID makes first-time setup easy (see docs/bot-setup.md).
            try:
                await update.message.reply_text(
                    f"This is a private bot. Your Telegram user ID is {user.id}."
                )
            except Exception:
                logger.exception("Failed to reply to unauthorised user")
        # Always stop, even if the reply failed, so no other handler sees the update.
        raise ApplicationHandlerStop

    return gate


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(formatting.HELP_TEXT)


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Placeholder: will call the timetable query and the model.
    await update.effective_message.reply_text("Coming soon.")


async def mylogs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reports = storage.recent_reports(
        context.bot_data[DB_PATH_KEY], update.effective_user.id, limit=10
    )
    await update.effective_message.reply_text(formatting.format_report_list(reports))


async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    parsed = parse_log_text(" ".join(context.args or []))
    if parsed.conflicts:
        await message.reply_text(
            f"Couldn't log that: {'; '.join(parsed.conflicts)}.\n"
            "Try again, or send /log on its own to use buttons."
        )
        return

    await _retire_keyboard(context, message.chat_id, context.user_data.pop(DRAFT_KEY, None))
    draft = LogDraft.from_parsed(parsed, raw_text=message.text)
    ignored = (
        f"\n(Didn't understand: {' '.join(parsed.unrecognised)})" if parsed.unrecognised else ""
    )
    if draft.is_complete:
        await message.reply_text(_save(draft, update, context) + ignored)
    else:
        await _send_prompt(message, draft, context, extra=ignored)


async def log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    draft: LogDraft | None = context.user_data.get(DRAFT_KEY)
    if draft is None or query.message is None or draft.message_id != query.message.message_id:
        # Don't edit the message: it may already show a saved report.
        await query.answer("That log is finished or expired. Send /log to start a new one.")
        return

    if query.data == CANCEL_DATA:
        context.user_data.pop(DRAFT_KEY, None)
        await query.answer()
        await query.edit_message_text("Cancelled.")
        return

    try:
        step, value = parse_callback_data(query.data)
        if step != draft.next_step():
            raise ValueError(f"expected {draft.next_step()}, got {step}")  # e.g. a double tap
        draft.apply(step, value)
    except ValueError as exc:
        logger.info("Ignoring callback %r: %s", query.data, exc)
        await query.answer()
        return

    await query.answer()
    if draft.is_complete:
        context.user_data.pop(DRAFT_KEY, None)
        await query.edit_message_text(_save(draft, update, context))
    else:
        await query.edit_message_text(_prompt_text(draft), reply_markup=_keyboard(draft))


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text: only meaningful as a typed departure time during /log."""
    message = update.effective_message
    draft: LogDraft | None = context.user_data.get(DRAFT_KEY)
    if draft is None:
        await message.reply_text("Not sure what to do with that. Send /help to see what I can do.")
        return
    if draft.next_step() != "time":
        await message.reply_text("Tap one of the buttons above, or /cancel.")
        return

    departure_time = parse_time(message.text)
    if departure_time is None:
        await message.reply_text("Couldn't read that as a time. Try something like 8:12 or 20:12.")
        return

    draft.departure_time = departure_time
    await _retire_keyboard(context, message.chat_id, draft)
    if draft.is_complete:
        context.user_data.pop(DRAFT_KEY, None)
        await message.reply_text(_save(draft, update, context))
    else:
        await _send_prompt(message, draft, context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    draft = context.user_data.pop(DRAFT_KEY, None)
    if draft is None:
        await message.reply_text("Nothing to cancel.")
        return
    await _retire_keyboard(context, message.chat_id, draft)
    await message.reply_text("Cancelled.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("Something went wrong; it's been logged.")
        except TelegramError:
            logger.exception("Failed to send error reply")


def _save(draft: LogDraft, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Store a complete draft and return the confirmation text."""
    report = storage.NewReport(
        telegram_user_id=update.effective_user.id,
        reported_at=datetime.now(UTC),
        station_code=draft.station_code,
        train_description=draft.train_description(),
        crowd_level=draft.crowd_level,
        note=draft.raw_text,
    )
    report_id = storage.insert_report(context.bot_data[DB_PATH_KEY], report)
    return f"Logged #{report_id}: {formatting.describe_draft(draft)}"


async def _send_prompt(
    message: Message, draft: LogDraft, context: ContextTypes.DEFAULT_TYPE, extra: str = ""
) -> None:
    sent = await message.reply_text(_prompt_text(draft) + extra, reply_markup=_keyboard(draft))
    draft.message_id = sent.message_id
    context.user_data[DRAFT_KEY] = draft


async def _retire_keyboard(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, draft: LogDraft | None
) -> None:
    """Remove the buttons from a draft's prompt so stale taps aren't offered."""
    if draft is None or draft.message_id is None:
        return
    # Fails if the message was already edited, deleted, or is too old; nothing to clean up then.
    with contextlib.suppress(TelegramError):
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=draft.message_id, reply_markup=None
        )


def _prompt_text(draft: LogDraft) -> str:
    so_far = formatting.describe_draft(draft)
    prompt = _PROMPTS[draft.next_step()]
    return f"{so_far}\n\n{prompt}" if so_far else prompt


def _keyboard(draft: LogDraft) -> InlineKeyboardMarkup:
    step = draft.next_step()
    if step == "station":
        buttons = [
            InlineKeyboardButton(s.name, callback_data=callback_data("station", s.code))
            for s in STATIONS
        ]
        rows = _chunk(buttons, 3)
    elif step == "time":
        labels = [t.strftime("%H:%M") for t in time_choices(datetime.now(IST))]
        rows = _chunk(
            [
                InlineKeyboardButton(label, callback_data=callback_data("time", label))
                for label in labels
            ],
            4,
        )
    elif step == "service":
        rows = [
            [
                InlineKeyboardButton(s.capitalize(), callback_data=callback_data("service", s))
                for s in SERVICES
            ]
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    f"{level} · {label}", callback_data=callback_data("crowd", str(level))
                )
            ]
            for level, label in CROWD_LEVELS.items()
        ]
    rows.append([InlineKeyboardButton("Cancel", callback_data=CANCEL_DATA)])
    return InlineKeyboardMarkup(rows)


def _chunk(buttons: list[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + size] for i in range(0, len(buttons), size)]
