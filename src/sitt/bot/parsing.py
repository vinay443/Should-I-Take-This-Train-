"""Free-text parsing for crowd reports. No Telegram imports here."""

import re
from dataclasses import dataclass
from datetime import time
from typing import Literal

from sitt.bot.stations import FALLBACK_DIRECTORY, StationDirectory

Service = Literal["fast", "slow"]
SERVICES: tuple[Service, ...] = ("fast", "slow")

CROWD_LEVELS: dict[int, str] = {
    1: "empty",
    2: "seats free",
    3: "standing",
    4: "packed",
    5: "can't board",
}

_CROWD_WORDS: dict[str, int] = {
    "empty": 1,
    "seats": 2,
    "seat": 2,
    "sitting": 2,
    "seated": 2,
    "standing": 3,
    "stand": 3,
    "packed": 4,
    "full": 4,
    "crowded": 4,
    "cantboard": 5,
}

_TIME_RE = re.compile(r"^(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?(?P<ampm>am|pm)?$")
_CANT_BOARD_RE = re.compile(r"\b(?:can'?t|cannot|can not|couldn'?t)\s*board\b")
_AMPM_RE = re.compile(r"(\d)\s+(am|pm)\b")


@dataclass(frozen=True)
class ParsedLog:
    """Whatever could be read from a quick `/log` message. Missing fields are None."""

    station_code: str | None = None
    departure_time: time | None = None
    service: Service | None = None
    crowd_level: int | None = None
    unrecognised: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def parse_time(text: str) -> time | None:
    """Parse `8:12`, `08.12`, `20:12`, `8:12pm`, `8 pm` and the like.

    A bare number such as `8` is not treated as a time, so it can't be
    confused with a crowd level.
    """
    match = _TIME_RE.match(text.strip().lower().replace(" ", ""))
    if match is None:
        return None
    minute_text, ampm = match["minute"], match["ampm"]
    if minute_text is None and ampm is None:
        return None
    hour, minute = int(match["hour"]), int(minute_text or 0)
    if ampm is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_crowd_level(token: str) -> int | None:
    """Parse `1`-`5` or a crowd word such as `packed`."""
    token = token.strip().lower()
    if token in {"1", "2", "3", "4", "5"}:
        return int(token)
    return _CROWD_WORDS.get(token)


def parse_service(token: str) -> Service | None:
    token = token.strip().lower()
    return token if token in SERVICES else None  # type: ignore[return-value]


def _tokenise(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    text = _CANT_BOARD_RE.sub("cantboard", text)
    text = _AMPM_RE.sub(r"\1\2", text)
    tokens = (raw.strip(".!?()\"'") for raw in re.split(r"[\s,;]+", text))
    return [t for t in tokens if t]


def parse_log_text(text: str, stations: StationDirectory = FALLBACK_DIRECTORY) -> ParsedLog:
    """Parse the arguments of a quick log, e.g. `8:12 fast KYN packed`.

    Tokens can come in any order. Unknown words are collected rather than
    rejected (the raw text is kept as the report's note anyway); two different
    values for the same field are reported as conflicts. Station names of two
    words, such as `Kanjur Marg`, are recognised.
    """
    found: dict[str, list] = {"station": [], "time": [], "service": [], "crowd": []}
    unrecognised: list[str] = []

    def add(field: str, value: object) -> None:
        if value not in found[field]:
            found[field].append(value)

    tokens = _tokenise(text)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        pair = stations.lookup(token + tokens[i + 1]) if i + 1 < len(tokens) else None
        if pair is not None:
            add("station", pair.code)
            i += 1
        elif (parsed_time := parse_time(token)) is not None:
            add("time", parsed_time)
        elif (level := parse_crowd_level(token)) is not None:
            add("crowd", level)
        elif (service := parse_service(token)) is not None:
            add("service", service)
        elif (station := stations.lookup(token)) is not None:
            add("station", station.code)
        else:
            unrecognised.append(token)
        i += 1

    conflicts = []
    if len(found["station"]) > 1:
        conflicts.append(f"more than one station ({', '.join(found['station'])})")
    if len(found["time"]) > 1:
        conflicts.append(
            f"more than one time ({', '.join(t.strftime('%H:%M') for t in found['time'])})"
        )
    if len(found["service"]) > 1:
        conflicts.append("both fast and slow")
    if len(found["crowd"]) > 1:
        conflicts.append(f"more than one crowd level ({', '.join(map(str, found['crowd']))})")

    def single(field: str):
        return found[field][0] if len(found[field]) == 1 else None

    return ParsedLog(
        station_code=single("station"),
        departure_time=single("time"),
        service=single("service"),
        crowd_level=single("crowd"),
        unrecognised=tuple(unrecognised),
        conflicts=tuple(conflicts),
    )
