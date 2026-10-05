"""Saved routes (`/fav`) and choosing one for `/commute`. No Telegram imports here."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

from sitt.bot.parsing import parse_time
from sitt.bot.schedule import split_stations
from sitt.bot.stations import StationDirectory
from sitt.config import CommuteSettings
from sitt.db import connect
from sitt.ingest.timetable import DAY_NAMES, parse_days

DEFAULT_WEEKDAYS = "YYYYYNN"  # Monday to Friday
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")
FLAGS = ("notify", "nudge")

ADD_USAGE = (
    "Save a route like this:\n"
    "/fav add work KYN CSMT 8:12 mon-fri\n"
    "The name comes first, then where from and where to. The time of your usual train and "
    "the days you travel are optional (the days default to mon-fri)."
)
USAGE = (
    "/fav add <name> <from> <to> [time] [days]: save a route\n"
    "/fav list: your saved routes\n"
    "/fav remove <name>: forget one\n"
    "/fav default <name>: the route /commute uses\n"
    "/fav notify <name> on|off: the message before your usual train\n"
    "/fav nudge <name> on|off: the crowding question after it"
)


class FavouriteError(ValueError):
    """A `/fav` request that can't be carried out. The message is shown to the user."""


@dataclass(frozen=True)
class Favourite:
    user_id: int
    name: str
    from_station: str
    to_station: str
    usual_departure: time | None = None
    weekdays: str = DEFAULT_WEEKDAYS
    is_default: bool = False
    notify: bool = True
    nudge: bool = True

    def runs_on(self, weekday: int) -> bool:
        """Whether the rider travels this route on `weekday` (Monday is 0)."""
        return self.weekdays[weekday] == "Y"


def describe_days(mask: str) -> str:
    """'YYYYYNN' -> 'mon-fri'; anything irregular as a list of days."""
    if mask == "YYYYYYY":
        return "daily"
    days = [name for name, flag in zip(DAY_NAMES, mask, strict=True) if flag == "Y"]
    first, last = DAY_NAMES.index(days[0]), DAY_NAMES.index(days[-1])
    if len(days) > 2 and last - first + 1 == len(days):
        return f"{days[0]}-{days[-1]}"
    return ", ".join(days)


def describe(favourite: Favourite, stations: StationDirectory) -> str:
    """One line for `/fav list`."""
    line = (
        f"{favourite.name}: {stations.label(favourite.from_station)} → "
        f"{stations.label(favourite.to_station)}"
    )
    if favourite.usual_departure is not None:
        line += f", usual train {favourite.usual_departure:%H:%M}"
        line += f", {describe_days(favourite.weekdays)}"
        off = [flag for flag in FLAGS if not getattr(favourite, flag)]
        if off:
            line += f" ({' and '.join(off)} off)"
    return line + (" [default]" if favourite.is_default else "")


def parse_add(user_id: int, args: Sequence[str], stations: StationDirectory) -> Favourite:
    """Read `/fav add work KYN CSMT 8:12 mon-fri`. Raises FavouriteError with what's wrong."""
    words = list(args)
    if len(words) < 3:
        raise FavouriteError(ADD_USAGE)
    name = words.pop(0).lower()
    if not NAME_RE.fullmatch(name):
        raise FavouriteError(
            f"{name!r} can't be a name. Use up to 20 letters, digits, - or _, e.g. work."
        )

    weekdays, usual = DEFAULT_WEEKDAYS, None
    # The optional days and time are the last one or two words, in either order.
    for _ in range(2):
        if len(words) <= 2:
            break
        last = words[-1].lower()
        if usual is None and (parsed := parse_time(last)) is not None:
            usual = parsed
        elif stations.lookup(last) is None and _looks_like_days(last):
            try:
                weekdays = parse_days(last.replace(",", "|"))
            except ValueError:
                raise FavouriteError(
                    f"I couldn't read {words[-1]!r} as days. Try mon-fri, mon-sat or daily."
                ) from None
        else:
            break
        words.pop()

    pair = split_stations(words, stations)
    if pair is None:
        raise FavouriteError(ADD_USAGE)
    origin, destination = (stations.lookup(text) for text in pair)
    if origin is None or destination is None:
        unknown = pair[0] if origin is None else pair[1]
        raise FavouriteError(f"I don't know a station called {unknown!r}.")
    if origin == destination:
        raise FavouriteError("Those are the same station.")
    return Favourite(user_id, name, origin.code, destination.code, usual, weekdays)


def _looks_like_days(word: str) -> bool:
    return word == "daily" or any(day in word for day in DAY_NAMES)


# --- storage ---

_COLUMNS = (
    "user_id, name, from_station, to_station, usual_departure, weekdays, is_default, notify, nudge"
)


def list_favourites(db_path: Path | str, user_id: int | None = None) -> list[Favourite]:
    """A user's favourites, the default first; or everyone's when `user_id` is None."""
    with connect(db_path) as con:
        return read_favourites(con, user_id)


def read_favourites(con, user_id: int | None = None) -> list[Favourite]:
    rows = con.execute(
        f"SELECT {_COLUMNS} FROM favourite_routes WHERE ? IS NULL OR user_id = ? "
        "ORDER BY user_id, is_default DESC, name",
        [user_id, user_id],
    ).fetchall()
    return [Favourite(*row) for row in rows]


def add_favourite(db_path: Path | str, favourite: Favourite) -> Favourite:
    """Save a favourite, replacing one of the same name. A user's first becomes the default."""
    with connect(db_path) as con:
        existing = read_favourites(con, favourite.user_id)
        same = next((f for f in existing if f.name == favourite.name), None)
        is_default = same.is_default if same else not existing
        con.execute(
            "INSERT OR REPLACE INTO favourite_routes "
            f"({_COLUMNS}, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                favourite.user_id,
                favourite.name,
                favourite.from_station,
                favourite.to_station,
                favourite.usual_departure,
                favourite.weekdays,
                is_default,
                same.notify if same else True,
                same.nudge if same else True,
                datetime.now(UTC),
            ],
        )
        return next(f for f in read_favourites(con, favourite.user_id) if f.name == favourite.name)


def remove_favourite(db_path: Path | str, user_id: int, name: str) -> bool:
    """Forget a favourite. If it was the default, another one takes over."""
    with connect(db_path) as con:
        existing = read_favourites(con, user_id)
        target = next((f for f in existing if f.name == name.lower()), None)
        if target is None:
            return False
        con.execute(
            "DELETE FROM favourite_routes WHERE user_id = ? AND name = ?", [user_id, target.name]
        )
        others = [f for f in existing if f.name != target.name]
        if target.is_default and others:
            con.execute(
                "UPDATE favourite_routes SET is_default = true WHERE user_id = ? AND name = ?",
                [user_id, others[0].name],
            )
        return True


def set_default(db_path: Path | str, user_id: int, name: str) -> bool:
    with connect(db_path) as con:
        if not any(f.name == name.lower() for f in read_favourites(con, user_id)):
            return False
        con.execute(
            "UPDATE favourite_routes SET is_default = (name = ?) WHERE user_id = ?",
            [name.lower(), user_id],
        )
        return True


def set_flag(db_path: Path | str, user_id: int, name: str, flag: str, value: bool) -> bool:
    """Switch a favourite's `notify` or `nudge` on or off. False if there is no such favourite."""
    if flag not in FLAGS:
        raise ValueError(f"unknown flag {flag!r}")
    with connect(db_path) as con:
        if not any(f.name == name.lower() for f in read_favourites(con, user_id)):
            return False
        con.execute(
            f"UPDATE favourite_routes SET {flag} = ? WHERE user_id = ? AND name = ?",
            [value, user_id, name.lower()],
        )
        return True


# --- /commute ---


def reverse_of(favourite: Favourite, favourites: Sequence[Favourite]) -> Favourite | None:
    """The same user's favourite that runs the other way, if there is one."""
    return next(
        (
            other
            for other in favourites
            if other.user_id == favourite.user_id
            and other.from_station == favourite.to_station
            and other.to_station == favourite.from_station
        ),
        None,
    )


def pick_for_commute(
    favourites: Sequence[Favourite], now: datetime, settings: CommuteSettings | None = None
) -> Favourite | None:
    """The favourite `/commute` should answer for, given one user's favourites.

    Normally the default. When the default and another favourite are each other's
    reverse, they are an outbound and a return leg: the outbound one is used before
    `return_after` and the return one from then on. The outbound leg is the one whose
    usual train is earlier in the day, or the default if that can't be told.
    """
    settings = settings or CommuteSettings()
    if not favourites:
        return None
    default = next((f for f in favourites if f.is_default), favourites[0])
    back = reverse_of(default, favourites)
    if back is None:
        return default
    outbound = default
    if (
        default.usual_departure is not None
        and back.usual_departure is not None
        and back.usual_departure < default.usual_departure
    ):
        outbound, back = back, default
    return back if now.time() >= settings.return_after else outbound
