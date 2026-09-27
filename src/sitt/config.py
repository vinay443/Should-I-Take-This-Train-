"""Settings read from environment variables, plus a `.env` file in the working directory."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

DEFAULT_DB_PATH = "data/sitt.duckdb"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str
    telegram_bot_token: str | None
    allowed_user_ids: frozenset[int]


def parse_allowed_user_ids(raw: str | None) -> frozenset[int]:
    """Parse Telegram user IDs separated by commas and/or whitespace, e.g. "123, 456"."""
    parts = [part for part in re.split(r"[,\s]+", raw or "") if part]
    for part in parts:
        if not re.fullmatch(r"\d+", part, re.ASCII):
            raise ValueError(f"ALLOWED_USER_IDS contains {part!r}, which is not a Telegram user ID")
    return frozenset(int(part) for part in parts)


def load_settings() -> Settings:
    # Variables already set in the environment take precedence over `.env`.
    load_dotenv(find_dotenv(usecwd=True))
    return Settings(
        db_path=Path(os.environ.get("SITT_DB_PATH") or DEFAULT_DB_PATH),
        log_level=os.environ.get("SITT_LOG_LEVEL") or "INFO",
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        allowed_user_ids=parse_allowed_user_ids(os.environ.get("ALLOWED_USER_IDS")),
    )
