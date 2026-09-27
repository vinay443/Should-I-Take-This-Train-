"""Settings read from environment variables, plus a `.env` file in the working directory."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

DEFAULT_DB_PATH = "data/sitt.duckdb"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str
    telegram_bot_token: str | None


def load_settings() -> Settings:
    # Variables already set in the environment take precedence over `.env`.
    load_dotenv(find_dotenv(usecwd=True))
    return Settings(
        db_path=Path(os.environ.get("SITT_DB_PATH") or DEFAULT_DB_PATH),
        log_level=os.environ.get("SITT_LOG_LEVEL") or "INFO",
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
    )
