"""Matching a logged report to a train, and letting the rider correct it.

The database work behind the bot's "Matched to the 08:12 fast to CSMT" line and its
buttons. No Telegram imports here. The matching itself is in `sitt.matching`.
"""

from dataclasses import dataclass
from pathlib import Path

from sitt import matching
from sitt.bot import storage
from sitt.config import MatchSettings
from sitt.db import connect
from sitt.matching import Candidate, MatchResult

CALLBACK_PREFIX = "match"
PICK, NONE, TRAIN = "pick", "none", "t"


def callback_data(report_id: int, action: str, train_id: str | None = None) -> str:
    """`match:<report id>:pick`, `match:<report id>:none` or `match:<report id>:t:<train id>`."""
    data = f"{CALLBACK_PREFIX}:{report_id}:{action}"
    return f"{data}:{train_id}" if train_id is not None else data


def parse_callback_data(data: str) -> tuple[int, str, str | None]:
    """Split callback data into (report id, action, train id). Raises ValueError if malformed."""
    parts = data.split(":", 3)
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX or not parts[1].isdigit():
        raise ValueError(f"bad callback data {data!r}")
    action = parts[2]
    if action == TRAIN and len(parts) == 4 and parts[3]:
        return int(parts[1]), TRAIN, parts[3]
    if action in (PICK, NONE) and len(parts) == 3:
        return int(parts[1]), action, None
    raise ValueError(f"bad callback data {data!r}")


def match_report(
    db_path: Path | str,
    report_id: int,
    settings: MatchSettings | None = None,
    ladies_special_ok: bool = False,
) -> MatchResult | None:
    """Match a report that was just logged and store the outcome."""
    with connect(db_path) as con:
        return matching.match_stored(
            con, report_id, matching.AUTO, settings, ladies_special_ok=ladies_special_ok
        )


@dataclass(frozen=True)
class Choices:
    """What the rider may pick from, or why they can't."""

    candidates: list[Candidate]
    problem: str | None = None  # shown to the rider instead, when set


def _owned_query(con, report_id: int, telegram_user_id: int):
    stored = matching.load_report(con, report_id)
    if stored is None or stored.source != storage.source_for(telegram_user_id):
        return None, "That isn't one of your reports."
    if stored.query is None:
        return None, "I can't read that report's train time."
    return stored.query, None


def choices_for(
    db_path: Path | str,
    report_id: int,
    telegram_user_id: int,
    settings: MatchSettings | None = None,
) -> Choices:
    """The trains near a report's time, for the rider to pick from.

    Ladies' specials are always offered here: the rider knows what they boarded.
    """
    settings = settings or MatchSettings()
    with connect(db_path) as con:
        query, problem = _owned_query(con, report_id, telegram_user_id)
        if query is None:
            return Choices([], problem)
        candidates = matching.find_candidates(con, query, settings, ladies_special_ok=True)
    candidates = sorted(candidates[: settings.choices], key=lambda c: (c.departure, c.train_id))
    if not candidates:
        return Choices([], "The timetable has no train near that time at that station.")
    return Choices(candidates)


def choose_train(
    db_path: Path | str,
    report_id: int,
    telegram_user_id: int,
    train_id: str | None,
    settings: MatchSettings | None = None,
) -> tuple[Candidate | None, str | None]:
    """Record the rider's own answer: a train, or (with None) "none of these".

    Returns (the chosen train, problem). The train must be one of those near the
    report's time, so a stale or forged button can't attach a report to any train.
    """
    settings = settings or MatchSettings()
    with connect(db_path) as con:
        query, problem = _owned_query(con, report_id, telegram_user_id)
        if query is None:
            return None, problem
        if train_id is None:
            matching.store_match(con, report_id, None, None, matching.USER_NONE)
            return None, None
        candidates = matching.find_candidates(con, query, settings, ladies_special_ok=True)
        chosen = next((c for c in candidates if c.train_id == train_id), None)
        if chosen is None:
            return None, "That train isn't near the time you logged."
        matching.store_match(con, report_id, train_id, 1.0, matching.USER)
        return chosen, None
