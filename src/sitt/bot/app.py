"""Application wiring and entry point (`uv run sitt-bot` or `python -m sitt.bot`)."""

import logging
import sys

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from sitt.bot import handlers
from sitt.bot.commute import CALLBACK_PREFIX as NUDGE_CALLBACK_PREFIX
from sitt.bot.flow import CALLBACK_PREFIX
from sitt.bot.matchflow import CALLBACK_PREFIX as MATCH_CALLBACK_PREFIX
from sitt.config import Settings, load_settings
from sitt.db import init_db

logger = logging.getLogger(__name__)

COMMUTE_JOB_NAME = "commute-messages"
COMMUTE_TICK_SECONDS = 60

COMMANDS = [
    BotCommand("log", "Log how crowded a train was"),
    BotCommand("mylogs", "Your last 10 reports"),
    BotCommand("next", "Which train to take, e.g. /next KYN CSMT"),
    BotCommand("why", "Explain the last /next recommendation"),
    BotCommand("commute", "/next for your saved route"),
    BotCommand("fav", "Saved routes: /fav add work KYN CSMT 8:12"),
    BotCommand("cancel", "Abandon a log in progress"),
    BotCommand("help", "What this bot does"),
]


def settings_problems(settings: Settings) -> list[str]:
    """Reasons the bot can't start with these settings. Empty means good to go."""
    problems = []
    if not settings.telegram_bot_token:
        problems.append("TELEGRAM_BOT_TOKEN is not set")
    if not settings.allowed_user_ids:
        # Fail closed: an empty allow-list must not mean "everyone".
        problems.append("ALLOWED_USER_IDS is empty")
    return problems


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(COMMANDS)


def build_application(settings: Settings) -> Application:
    application = (
        Application.builder().token(settings.telegram_bot_token).post_init(_post_init).build()
    )
    application.bot_data[handlers.DB_PATH_KEY] = settings.db_path
    application.bot_data[handlers.MODEL_DIR_KEY] = settings.model_dir
    application.bot_data[handlers.RECOMMEND_SETTINGS_KEY] = settings.recommend
    application.bot_data[handlers.MATCH_SETTINGS_KEY] = settings.match
    application.bot_data[handlers.COMMUTE_SETTINGS_KEY] = settings.commute
    application.bot_data[handlers.ALLOWED_USERS_KEY] = settings.allowed_user_ids

    # Group -1 runs first; the gate stops every update from users not on the list.
    gate = handlers.make_access_gate(settings.allowed_user_ids)
    application.add_handler(TypeHandler(Update, gate), group=-1)
    application.add_handlers(
        [
            CommandHandler(["start", "help"], handlers.help_command),
            CommandHandler("log", handlers.log_command),
            CommandHandler("mylogs", handlers.mylogs_command),
            CommandHandler("next", handlers.next_command),
            CommandHandler("why", handlers.why_command),
            CommandHandler("commute", handlers.commute_command),
            CommandHandler(["fav", "favs"], handlers.fav_command),
            CommandHandler("cancel", handlers.cancel_command),
            CallbackQueryHandler(handlers.log_callback, pattern=rf"^{CALLBACK_PREFIX}:"),
            CallbackQueryHandler(handlers.match_callback, pattern=rf"^{MATCH_CALLBACK_PREFIX}:"),
            CallbackQueryHandler(handlers.nudge_callback, pattern=rf"^{NUDGE_CALLBACK_PREFIX}:"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_message),
        ]
    )
    application.add_error_handler(handlers.on_error)
    schedule_commute_messages(application, settings)
    return application


def schedule_commute_messages(application: Application, settings: Settings) -> bool:
    """Start the once-a-minute check for morning messages and crowd prompts.

    Returns whether it was scheduled. It isn't when both are switched off, or when
    python-telegram-bot was installed without its job-queue extra.
    """
    commute = settings.commute
    if not (commute.notify_enabled or commute.nudge_enabled):
        return False
    if application.job_queue is None:
        logger.warning(
            "No job queue (install python-telegram-bot[job-queue]): the morning message and "
            "the crowd prompt are off."
        )
        return False
    application.job_queue.run_repeating(
        handlers.commute_tick, interval=COMMUTE_TICK_SECONDS, first=10, name=COMMUTE_JOB_NAME
    )
    return True


def main() -> None:
    try:
        settings = load_settings()
    except ValueError as exc:
        sys.exit(f"Config error: {exc}")
    if problems := settings_problems(settings):
        sys.exit(f"Can't start the bot: {'; '.join(problems)}. See docs/bot-setup.md.")

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=settings.log_level
    )
    # httpx logs every request URL at INFO, and Bot API URLs contain the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db(settings.db_path).close()
    logger.info(
        "Starting bot (polling) for %d allowed user(s), database at %s",
        len(settings.allowed_user_ids),
        settings.db_path,
    )
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)
