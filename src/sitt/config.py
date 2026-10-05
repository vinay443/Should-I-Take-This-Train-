"""Settings read from environment variables, plus a `.env` file in the working directory."""

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

DEFAULT_DB_PATH = "data/sitt.duckdb"
DEFAULT_MODEL_DIR = "models/delay"


@dataclass(frozen=True)
class RecommendSettings:
    """Thresholds for "take this train or wait?" (see sitt.recommend).

    Each can be set with the environment variable named beside it.
    """

    candidates: int = 5  # SITT_RECOMMEND_CANDIDATES: how many upcoming trains to compare
    # SITT_WAIT_MAX_EXTRA_MINUTES: a later, emptier train is worth waiting for only if it
    # arrives no more than this long after the earliest arrival.
    wait_max_extra_minutes: float = 5.0
    # SITT_CROWD_GAIN_LEVELS: and only if it is at least this many crowding levels emptier.
    crowd_gain_levels: int = 1
    # SITT_VERY_LATE_MINUTES: a train predicted this late at the boarding station is
    # "very late": too unreliable to recommend if another arrives soon after.
    very_late_minutes: float = 15.0
    # SITT_VERY_LATE_SLACK_MINUTES: "soon after" for the rule above.
    very_late_slack_minutes: float = 10.0
    # SITT_LADIES_SPECIAL_OK: set to true if the rider can board ladies' specials.
    # Otherwise they are listed but never recommended.
    ladies_special_ok: bool = False
    # SITT_ALLOW_SYNTHETIC_MODEL: use a delay model even if it was trained on synthetic
    # (invented) data. For testing only. Off by default, so such a model is ignored and
    # predictions fall back to past observations or the timetable.
    allow_synthetic_model: bool = False


@dataclass(frozen=True)
class BlockSettings:
    """How megablocks are interpreted (see sitt.models.features and docs/megablocks.md).

    A block announced without times is assumed to run between these hours, Mumbai time,
    which is when Sunday megablocks usually are. Set with SITT_BLOCK_DEFAULT_START and
    SITT_BLOCK_DEFAULT_END, as HH:MM.
    """

    default_start: time = time(10, 0)
    default_end: time = time(16, 0)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str
    telegram_bot_token: str | None
    allowed_user_ids: frozenset[int]
    model_dir: Path = Path(DEFAULT_MODEL_DIR)
    recommend: RecommendSettings = field(default_factory=RecommendSettings)


def parse_allowed_user_ids(raw: str | None) -> frozenset[int]:
    """Parse Telegram user IDs separated by commas and/or whitespace, e.g. "123, 456"."""
    parts = [part for part in re.split(r"[,\s]+", raw or "") if part]
    for part in parts:
        if not re.fullmatch(r"\d+", part, re.ASCII):
            raise ValueError(f"ALLOWED_USER_IDS contains {part!r}, which is not a Telegram user ID")
    return frozenset(int(part) for part in parts)


def _number(name: str, default: float, kind: type = float):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"{name} is {raw!r}, which is not a number") from None
    if value < 0:
        raise ValueError(f"{name} is {raw!r}; it can't be negative")
    return value


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def load_recommend_settings() -> RecommendSettings:
    defaults = RecommendSettings()
    return RecommendSettings(
        candidates=max(1, _number("SITT_RECOMMEND_CANDIDATES", defaults.candidates, int)),
        wait_max_extra_minutes=_number(
            "SITT_WAIT_MAX_EXTRA_MINUTES", defaults.wait_max_extra_minutes
        ),
        crowd_gain_levels=_number("SITT_CROWD_GAIN_LEVELS", defaults.crowd_gain_levels, int),
        very_late_minutes=_number("SITT_VERY_LATE_MINUTES", defaults.very_late_minutes),
        very_late_slack_minutes=_number(
            "SITT_VERY_LATE_SLACK_MINUTES", defaults.very_late_slack_minutes
        ),
        ladies_special_ok=_flag("SITT_LADIES_SPECIAL_OK"),
        allow_synthetic_model=_flag("SITT_ALLOW_SYNTHETIC_MODEL"),
    )


def _clock(name: str, default: time) -> time:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return time.fromisoformat(raw if len(raw) > 4 else f"0{raw}")
    except ValueError:
        raise ValueError(f"{name} is {raw!r}, which is not a time like 10:00") from None


def load_block_settings() -> BlockSettings:
    defaults = BlockSettings()
    return BlockSettings(
        default_start=_clock("SITT_BLOCK_DEFAULT_START", defaults.default_start),
        default_end=_clock("SITT_BLOCK_DEFAULT_END", defaults.default_end),
    )


def load_settings() -> Settings:
    # Variables already set in the environment take precedence over `.env`.
    load_dotenv(find_dotenv(usecwd=True))
    return Settings(
        db_path=Path(os.environ.get("SITT_DB_PATH") or DEFAULT_DB_PATH),
        log_level=os.environ.get("SITT_LOG_LEVEL") or "INFO",
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        allowed_user_ids=parse_allowed_user_ids(os.environ.get("ALLOWED_USER_IDS")),
        model_dir=Path(os.environ.get("SITT_MODEL_DIR") or DEFAULT_MODEL_DIR),
        recommend=load_recommend_settings(),
    )
