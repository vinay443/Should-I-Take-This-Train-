"""Telegram handlers. Kept thin: parsing and flow logic live in plain modules."""

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import duckdb
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from sitt.bot import commute, favourites, formatting, matchflow, schedule, storage
from sitt.bot.flow import (
    CANCEL_DATA,
    LogDraft,
    Step,
    callback_data,
    parse_callback_data,
    time_choices,
)
from sitt.bot.parsing import CROWD_LEVELS, SERVICES, parse_log_text, parse_time
from sitt.bot.stations import StationDirectory, load_directory
from sitt.config import CommuteSettings
from sitt.db import connect
from sitt.recommend import explain
from sitt.tz import IST

logger = logging.getLogger(__name__)

DRAFT_KEY = "log_draft"
DB_PATH_KEY = "db_path"
MODEL_DIR_KEY = "model_dir"  # bot_data: where a trained delay model may be, or None
RECOMMEND_SETTINGS_KEY = "recommend_settings"  # bot_data: thresholds for /next
LAST_RECOMMENDATION_KEY = "last_recommendation"  # user_data: what /why explains
MATCH_SETTINGS_KEY = "match_settings"  # bot_data: how reports are matched to trains
COMMUTE_SETTINGS_KEY = "commute_settings"  # bot_data: /commute and the bot's own messages
ALLOWED_USERS_KEY = "allowed_user_ids"  # bot_data: who the bot may message on its own

_PROMPTS: dict[Step, str] = {
    "station": "Where did you board? Tap a station or type its name.",
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
    message = update.effective_message
    pair = schedule.split_stations(context.args or [], _stations(context))
    if pair is None:
        await message.reply_text(formatting.NEXT_USAGE)
        return
    now = datetime.now(IST)
    try:
        recommendation = schedule.recommend_trains(
            context.bot_data[DB_PATH_KEY],
            *pair,
            now,
            settings=context.bot_data.get(RECOMMEND_SETTINGS_KEY),
            model_dir=context.bot_data.get(MODEL_DIR_KEY),
        )
    except schedule.ScheduleError as exc:
        await message.reply_text(f"{exc}\n{formatting.NEXT_USAGE}")
        return
    context.user_data[LAST_RECOMMENDATION_KEY] = recommendation
    await message.reply_text(formatting.format_recommendation(recommendation))


async def why_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain the last `/next` recommendation in more detail."""
    recommendation = context.user_data.get(LAST_RECOMMENDATION_KEY)
    if recommendation is None:
        await update.effective_message.reply_text(formatting.NO_RECOMMENDATION_YET)
        return
    await update.effective_message.reply_text(explain(recommendation))


# --- saved routes, /commute, and the messages the bot sends on its own ---


def _commute_settings(context: ContextTypes.DEFAULT_TYPE) -> CommuteSettings:
    return context.bot_data.get(COMMUTE_SETTINGS_KEY) or CommuteSettings()


async def fav_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/fav add|list|remove|default|notify|nudge`: saved routes."""
    message = update.effective_message
    user_id = update.effective_user.id
    db_path = context.bot_data[DB_PATH_KEY]
    args = list(context.args or [])
    action = args[0].lower() if args else "list"
    stations = _stations(context)

    if action == "list":
        saved = favourites.list_favourites(db_path, user_id)
        if not saved:
            await message.reply_text(f"No saved routes yet.\n{favourites.ADD_USAGE}")
            return
        lines = ["Your saved routes:"]
        lines += [f"• {favourites.describe(favourite, stations)}" for favourite in saved]
        await message.reply_text("\n".join(lines))
        return

    if action == "add":
        try:
            parsed = favourites.parse_add(user_id, args[1:], stations)
        except favourites.FavouriteError as exc:
            await message.reply_text(str(exc))
            return
        saved = favourites.add_favourite(db_path, parsed)
        await message.reply_text(_saved_text(saved, context, stations))
        return

    name = args[1].lower() if len(args) > 1 else None
    if action in ("remove", "rm", "delete") and name:
        removed = favourites.remove_favourite(db_path, user_id, name)
        await message.reply_text(f"Removed {name}." if removed else _no_such(name))
    elif action == "default" and name:
        done = favourites.set_default(db_path, user_id, name)
        await message.reply_text(f"/commute now uses {name}." if done else _no_such(name))
    elif action in favourites.FLAGS and name and len(args) > 2 and args[2].lower() in ("on", "off"):
        value = args[2].lower() == "on"
        done = favourites.set_flag(db_path, user_id, name, action, value)
        what = "The message before your train" if action == "notify" else "The crowding question"
        await message.reply_text(
            f"{what} is {'on' if value else 'off'} for {name}." if done else _no_such(name)
        )
    else:
        await message.reply_text(favourites.USAGE)


def _no_such(name: str) -> str:
    return f"You have no saved route called {name!r}. /fav list shows them."


def _saved_text(
    saved: favourites.Favourite, context: ContextTypes.DEFAULT_TYPE, stations: StationDirectory
) -> str:
    """Confirmation for `/fav add`, saying what the bot will now do by itself."""
    settings = _commute_settings(context)
    lines = [f"Saved {favourites.describe(saved, stations)}"]
    if saved.usual_departure is None:
        lines.append("No usual train time, so I won't message you about it. /commute works.")
        return "\n".join(lines)
    today = datetime.now(IST).date()
    days = [today + timedelta(days=offset) for offset in range(7)]
    day = next((d for d in days if saved.runs_on(d.weekday())), today)
    with connect(context.bot_data[DB_PATH_KEY]) as con:
        trip = commute.usual_trip(con, saved, day, settings)
    if settings.notify_enabled:
        lines.append(
            f"On those days I'll send the recommendation {settings.notify_lead_minutes:g} min "
            f"before. /fav notify {saved.name} off stops that."
        )
    if trip is None:
        lines.append(
            f"The timetable has no train within {settings.usual_train_minutes:g} min of "
            f"{saved.usual_departure:%H:%M} on this route, so I won't ask how crowded it was."
        )
    elif settings.nudge_enabled:
        lines.append(
            f"After the {trip.departure:%H:%M} {trip.train_type} to {trip.label} arrives I'll ask "
            f"how crowded it was. /fav nudge {saved.name} off stops that."
        )
    lines.append("Both only happen while this bot is running.")
    return "\n".join(lines)


async def commute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/next` for the default saved route, or its return leg later in the day."""
    message = update.effective_message
    db_path = context.bot_data[DB_PATH_KEY]
    saved = favourites.list_favourites(db_path, update.effective_user.id)
    now = datetime.now(IST)
    chosen = favourites.pick_for_commute(saved, now, _commute_settings(context))
    if chosen is None:
        await message.reply_text(f"No saved routes yet.\n{favourites.ADD_USAGE}")
        return
    try:
        recommendation = _recommend(context, chosen, now)
    except schedule.ScheduleError as exc:
        await message.reply_text(
            f"{exc}\nYour saved route {chosen.name!r} may need saving again with /fav add."
        )
        return
    context.user_data[LAST_RECOMMENDATION_KEY] = recommendation
    await message.reply_text(f"{chosen.name}\n{formatting.format_recommendation(recommendation)}")


def _recommend(context: ContextTypes.DEFAULT_TYPE, favourite: favourites.Favourite, now: datetime):
    return schedule.recommend_trains(
        context.bot_data[DB_PATH_KEY],
        favourite.from_station,
        favourite.to_station,
        now,
        settings=context.bot_data.get(RECOMMEND_SETTINGS_KEY),
        model_dir=context.bot_data.get(MODEL_DIR_KEY),
    )


def nudge_keyboard(item: commute.Due) -> InlineKeyboardMarkup:
    """1-5 in one row, for a one-tap answer; then the two ways out."""
    name = item.favourite.name
    levels = [
        InlineKeyboardButton(
            str(level), callback_data=commute.callback_data(item.day, str(level), name)
        )
        for level in CROWD_LEVELS
    ]
    others = [
        InlineKeyboardButton(
            "Didn't take it", callback_data=commute.callback_data(item.day, commute.SKIP, name)
        ),
        InlineKeyboardButton(
            "Stop asking", callback_data=commute.callback_data(item.day, commute.OFF, name)
        ),
    ]
    return InlineKeyboardMarkup([levels, others])


def nudge_text(item: commute.Due, stations: StationDirectory) -> str:
    trip = item.trip
    scale = " · ".join(f"{level} {label}" for level, label in CROWD_LEVELS.items())
    return (
        f"How crowded was the {trip.departure:%H:%M} {trip.train_type} to {trip.label} from "
        f"{stations.label(item.favourite.from_station)}?\n{scale}"
    )


async def commute_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every minute: send whatever morning messages and crowd prompts are due.

    Only users on the allow-list are ever messaged. A busy database just means trying
    again at the next tick, within the grace period.
    """
    db_path = context.bot_data[DB_PATH_KEY]
    now = datetime.now(IST)
    try:
        items = commute.check_due(
            db_path,
            now.replace(tzinfo=None),
            _commute_settings(context),
            context.bot_data.get(ALLOWED_USERS_KEY) or frozenset(),
        )
        stations = load_directory(db_path) if items else None
    except duckdb.Error as exc:
        logger.warning("Could not check for due messages (%s); will retry", type(exc).__name__)
        return
    for item in items:
        user_id = item.favourite.user_id
        try:
            if item.kind == commute.NOTIFY:
                recommendation = _recommend(context, item.favourite, now)
                text = (
                    f"Your {item.favourite.name} commute\n"
                    f"{formatting.format_recommendation(recommendation)}"
                )
                markup = None
            else:
                recommendation = None
                text, markup = nudge_text(item, stations), nudge_keyboard(item)
            # Recorded before sending: a message lost to an error is better than one
            # repeated every minute.
            commute.record_sent(db_path, item)
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=markup)
            if recommendation is not None:
                remembered = getattr(context.application, "user_data", None)
                if remembered is not None:
                    remembered[user_id][LAST_RECOMMENDATION_KEY] = recommendation
        except Exception:
            logger.exception("Could not send the %s message for %s", item.kind, item.favourite.name)


async def nudge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap under the crowd prompt: log the level, skip, or stop being asked."""
    query = update.callback_query
    try:
        day, answer, name = commute.parse_callback_data(query.data)
    except ValueError:
        await query.answer()
        return
    db_path = context.bot_data[DB_PATH_KEY]
    result = commute.answer_nudge(db_path, update.effective_user.id, day, name, answer)
    if result.status == "unknown":
        await query.answer("That question has expired.", show_alert=True)
        return
    if result.status == "already":
        await query.answer("Already answered.")
        return
    await query.answer()
    if result.status == "logged":
        level = int(answer)
        await query.edit_message_text(
            f"Logged #{result.report_id}: {result.description} · {formatting.crowd_label(level)}, "
            "matched to that train. Thanks."
        )
    elif result.status == "skipped":
        await query.edit_message_text(f"OK, nothing logged for the {result.description}.")
    else:
        await query.edit_message_text(
            f"I won't ask about your {name} commute again. /fav nudge {name} on turns it back on."
        )


async def mylogs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reports = storage.recent_reports(
        context.bot_data[DB_PATH_KEY], update.effective_user.id, limit=10
    )
    await update.effective_message.reply_text(formatting.format_report_list(reports))


async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    stations = _stations(context)
    parsed = parse_log_text(" ".join(context.args or []), stations)
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
        text, markup = _save(draft, update, context, stations)
        await message.reply_text(text + ignored, reply_markup=markup)
    else:
        await _send_prompt(message, draft, context, stations, extra=ignored)


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

    stations = _stations(context)
    try:
        step, value = parse_callback_data(query.data)
        if step != draft.next_step():
            raise ValueError(f"expected {draft.next_step()}, got {step}")  # e.g. a double tap
        draft.apply(step, value, stations)
    except ValueError as exc:
        logger.info("Ignoring callback %r: %s", query.data, exc)
        await query.answer()
        return

    await query.answer()
    if draft.is_complete:
        context.user_data.pop(DRAFT_KEY, None)
        text, markup = _save(draft, update, context, stations)
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await query.edit_message_text(
            _prompt_text(draft, stations), reply_markup=_keyboard(draft, stations)
        )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text: only meaningful as a typed station or departure time during /log."""
    message = update.effective_message
    draft: LogDraft | None = context.user_data.get(DRAFT_KEY)
    if draft is None:
        await message.reply_text("Not sure what to do with that. Send /help to see what I can do.")
        return
    stations = _stations(context)
    step = draft.next_step()
    if step == "station":
        station = stations.lookup(message.text)
        if station is None:
            await message.reply_text(
                "I don't know that station. Tap one above, or type its name or code (e.g. KYN)."
            )
            return
        draft.station_code = station.code
    elif step == "time":
        departure_time = parse_time(message.text)
        if departure_time is None:
            await message.reply_text(
                "Couldn't read that as a time. Try something like 8:12 or 20:12."
            )
            return
        draft.departure_time = departure_time
    else:
        await message.reply_text("Tap one of the buttons above, or /cancel.")
        return

    await _retire_keyboard(context, message.chat_id, draft)
    if draft.is_complete:
        context.user_data.pop(DRAFT_KEY, None)
        text, markup = _save(draft, update, context, stations)
        await message.reply_text(text, reply_markup=markup)
    else:
        await _send_prompt(message, draft, context, stations)


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


def _stations(context: ContextTypes.DEFAULT_TYPE) -> StationDirectory:
    """The station directory, read fresh so a newly loaded timetable is picked up."""
    return load_directory(context.bot_data[DB_PATH_KEY])


def _save(
    draft: LogDraft,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    stations: StationDirectory,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Store a complete draft. Returns the confirmation text and any buttons to go with it.

    The report is then matched to a scheduled train (sitt.matching). Whatever happens
    there, the report itself is already saved.
    """
    report = storage.NewReport(
        telegram_user_id=update.effective_user.id,
        reported_at=datetime.now(UTC),
        station_code=draft.station_code,
        train_description=draft.train_description(),
        crowd_level=draft.crowd_level,
        note=draft.raw_text,
    )
    db_path = context.bot_data[DB_PATH_KEY]
    report_id = storage.insert_report(db_path, report)
    text = f"Logged #{report_id}: {formatting.describe_draft(draft, stations)}"
    try:
        result = matchflow.match_report(
            db_path,
            report_id,
            context.bot_data.get(MATCH_SETTINGS_KEY),
            ladies_special_ok=_ladies_special_ok(context),
        )
    except Exception:  # the report is saved; matching can be redone with sitt-match-logs
        logger.exception("Could not match report %s to a train", report_id)
        return text, None
    if result is None:
        return text, None
    if result.train is not None:
        button = InlineKeyboardButton(
            "Not that train", callback_data=matchflow.callback_data(report_id, matchflow.PICK)
        )
        return f"{text}\nMatched to the {result.train.describe()}.", InlineKeyboardMarkup(
            [[button]]
        )
    if result.candidates:
        return (
            f"{text}\nI couldn't tell which train that was. Tap it if you know:",
            _train_buttons(report_id, result.candidates),
        )
    return text, None


def _ladies_special_ok(context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = context.bot_data.get(RECOMMEND_SETTINGS_KEY)
    return bool(settings and settings.ladies_special_ok)


def _train_buttons(report_id: int, candidates) -> InlineKeyboardMarkup:
    """One button per train, in time order, and a way to say it was none of them."""
    rows = [
        [
            InlineKeyboardButton(
                candidate.describe(),
                callback_data=matchflow.callback_data(
                    report_id, matchflow.TRAIN, candidate.train_id
                ),
            )
        ]
        for candidate in sorted(candidates, key=lambda c: (c.departure, c.train_id))
    ]
    rows.append(
        [
            InlineKeyboardButton(
                "None of these", callback_data=matchflow.callback_data(report_id, matchflow.NONE)
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def match_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buttons under a logged report: pick the train it was, or say it was none of them."""
    query = update.callback_query
    try:
        report_id, action, train_id = matchflow.parse_callback_data(query.data)
    except ValueError:
        await query.answer()
        return
    db_path = context.bot_data[DB_PATH_KEY]
    settings = context.bot_data.get(MATCH_SETTINGS_KEY)
    user_id = update.effective_user.id
    # The first line is the "Logged #12: ..." confirmation; what follows is about the match.
    logged_line = (query.message.text or "").split("\n")[0] if query.message else ""

    if action == matchflow.PICK:
        choices = matchflow.choices_for(db_path, report_id, user_id, settings)
        if choices.problem:
            await query.answer(choices.problem, show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            f"{logged_line}\nWhich train was it?",
            reply_markup=_train_buttons(report_id, choices.candidates),
        )
        return

    chosen, problem = matchflow.choose_train(db_path, report_id, user_id, train_id, settings)
    if problem:
        await query.answer(problem, show_alert=True)
        return
    await query.answer()
    if chosen is None:
        await query.edit_message_text(f"{logged_line}\nNot matched to a train.")
    else:
        await query.edit_message_text(f"{logged_line}\nTrain: {chosen.describe()} (your pick).")


async def _send_prompt(
    message: Message,
    draft: LogDraft,
    context: ContextTypes.DEFAULT_TYPE,
    stations: StationDirectory,
    extra: str = "",
) -> None:
    sent = await message.reply_text(
        _prompt_text(draft, stations) + extra, reply_markup=_keyboard(draft, stations)
    )
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


def _prompt_text(draft: LogDraft, stations: StationDirectory) -> str:
    so_far = formatting.describe_draft(draft, stations)
    prompt = _PROMPTS[draft.next_step()]
    return f"{so_far}\n\n{prompt}" if so_far else prompt


def _keyboard(draft: LogDraft, stations: StationDirectory) -> InlineKeyboardMarkup:
    step = draft.next_step()
    if step == "station":
        buttons = [
            InlineKeyboardButton(s.name, callback_data=callback_data("station", s.code))
            for s in stations.stations
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
