"""Wiring smoke tests. Building the Application makes no network calls."""

from pathlib import Path

from telegram.ext import CommandHandler

from sitt.bot.app import build_application, settings_problems
from sitt.config import Settings


def _settings(token: str | None = "123456:TEST-TOKEN", allowed=frozenset({42})) -> Settings:
    return Settings(
        db_path=Path("unused.duckdb"),
        log_level="INFO",
        telegram_bot_token=token,
        allowed_user_ids=allowed,
    )


def test_build_application_registers_commands():
    application = build_application(_settings())

    commands = {
        command
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert commands == {"start", "help", "log", "mylogs", "next", "cancel"}
    assert len(application.handlers[-1]) == 1  # access gate runs before everything else


def test_settings_problems():
    assert settings_problems(_settings()) == []
    assert settings_problems(_settings(token=None)) == ["TELEGRAM_BOT_TOKEN is not set"]
    assert settings_problems(_settings(allowed=frozenset())) == ["ALLOWED_USER_IDS is empty"]
