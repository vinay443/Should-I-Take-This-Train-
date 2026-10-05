"""Messages the bot sends on its own: the morning recommendation and the crowd prompt.

Both hang off a favourite route that has a usual departure time (see sitt.bot.favourites):

* **notify**: `notify_lead_minutes` before the usual train, the `/commute` recommendation.
* **nudge**: `nudge_after_minutes` after the usual train's scheduled arrival, a one-tap
  "how crowded was it?" whose answer is stored as a crowd report already tied to that
  train (`match_method = 'nudge'`).

`due` works out what should go out at a given moment, and `bot_notifications` records
what has gone, so each message is sent once a day even if the bot restarts. A message
whose moment was missed by more than `grace_minutes` is skipped: the bot only sends
while it is running. No Telegram imports here. All times are Mumbai local time.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from sitt import matching
from sitt.bot import storage
from sitt.bot.favourites import Favourite, read_favourites
from sitt.config import CommuteSettings
from sitt.db import connect
from sitt.holidays import is_sunday_schedule
from sitt.timetable import ScheduledTrip, UnknownStationError, next_trains

NOTIFY, NUDGE = "notify", "nudge"
CALLBACK_PREFIX = "nudge"
SKIP, OFF = "skip", "off"  # the two non-level answers


@dataclass(frozen=True)
class Due:
    """One message to send now."""

    kind: str  # NOTIFY or NUDGE
    favourite: Favourite
    day: date  # the day the usual train leaves
    trip: ScheduledTrip | None = None  # the usual train (always set for a nudge)


def usual_trip(
    con: duckdb.DuckDBPyConnection, favourite: Favourite, day: date, settings: CommuteSettings
) -> ScheduledTrip | None:
    """The scheduled train nearest the favourite's usual time on `day`, if one is close."""
    if favourite.usual_departure is None:
        return None
    usual = datetime.combine(day, favourite.usual_departure)
    reach = timedelta(minutes=settings.usual_train_minutes)
    try:
        trips = next_trains(con, favourite.from_station, favourite.to_station, usual - reach, n=6)
    except (UnknownStationError, ValueError):
        return None  # a station that is no longer in the timetable
    near = [trip for trip in trips if abs(trip.departure - usual) <= reach]
    return min(near, key=lambda trip: abs(trip.departure - usual), default=None)


def _already_sent(con: duckdb.DuckDBPyConnection, due: Due) -> bool:
    return (
        con.execute(
            "SELECT count(*) FROM bot_notifications "
            "WHERE user_id = ? AND kind = ? AND ref = ? AND day = ?",
            [due.favourite.user_id, due.kind, due.favourite.name, due.day],
        ).fetchone()[0]
        > 0
    )


def due(
    con: duckdb.DuckDBPyConnection,
    now: datetime,
    settings: CommuteSettings | None = None,
    allowed_user_ids: frozenset[int] | None = None,
) -> list[Due]:
    """What should be sent at `now` (naive Mumbai time) and hasn't been yet.

    Only favourites with a usual time, on the days their rider travels, for users in
    `allowed_user_ids` (None means anyone). With `skip_sunday_schedule`, nothing is due
    on a Sunday or a holiday.
    """
    settings = settings or CommuteSettings()
    grace = timedelta(minutes=settings.grace_minutes)
    out = []
    for favourite in read_favourites(con):
        if favourite.usual_departure is None:
            continue
        if allowed_user_ids is not None and favourite.user_id not in allowed_user_ids:
            continue
        # The usual train may have left yesterday (and arrived after midnight), or leave
        # tomorrow (with its reminder due just before midnight today).
        for offset in (-1, 0, 1):
            day = now.date() + timedelta(days=offset)
            if not favourite.runs_on(day.weekday()):
                continue
            if settings.skip_sunday_schedule and is_sunday_schedule(day):
                continue
            if settings.notify_enabled and favourite.notify:
                moment = datetime.combine(day, favourite.usual_departure) - timedelta(
                    minutes=settings.notify_lead_minutes
                )
                candidate = Due(NOTIFY, favourite, day)
                if moment <= now < moment + grace and not _already_sent(con, candidate):
                    out.append(candidate)
            if settings.nudge_enabled and favourite.nudge:
                trip = usual_trip(con, favourite, day, settings)
                if trip is None:
                    continue
                moment = trip.arrival + timedelta(minutes=settings.nudge_after_minutes)
                candidate = Due(NUDGE, favourite, day, trip)
                if moment <= now < moment + grace and not _already_sent(con, candidate):
                    out.append(candidate)
    return out


def train_description(trip: ScheduledTrip, station_code: str) -> str:
    """`crowd_reports.train_description` for a trip, in the form `/log` writes."""
    return f"{trip.departure:%H:%M} {trip.train_type} from {station_code}"


def mark_sent(con: duckdb.DuckDBPyConnection, item: Due, now: datetime | None = None) -> None:
    trip = item.trip
    con.execute(
        "INSERT OR REPLACE INTO bot_notifications "
        "(user_id, kind, ref, day, sent_at, train_id, station_code, description, answered) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, false)",
        [
            item.favourite.user_id,
            item.kind,
            item.favourite.name,
            item.day,
            now or datetime.now(UTC),
            trip.train_id if trip else None,
            item.favourite.from_station if trip else None,
            train_description(trip, item.favourite.from_station) if trip else None,
        ],
    )


def check_due(
    db_path: Path | str,
    now: datetime,
    settings: CommuteSettings | None = None,
    allowed_user_ids: frozenset[int] | None = None,
) -> list[Due]:
    with connect(db_path) as con:
        return due(con, now, settings, allowed_user_ids)


def record_sent(db_path: Path | str, item: Due) -> None:
    with connect(db_path) as con:
        mark_sent(con, item)


# --- the nudge's buttons ---


def callback_data(day: date, answer: str, name: str) -> str:
    """`nudge:<yyyymmdd>:<1-5 | skip | off>:<favourite name>`."""
    return f"{CALLBACK_PREFIX}:{day:%Y%m%d}:{answer}:{name}"


def parse_callback_data(data: str) -> tuple[date, str, str]:
    """Split callback data into (day, answer, favourite name). Raises ValueError if malformed."""
    parts = data.split(":", 3)
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX or not parts[3]:
        raise ValueError(f"bad callback data {data!r}")
    if parts[2] not in ("1", "2", "3", "4", "5", SKIP, OFF):
        raise ValueError(f"bad callback data {data!r}")
    return datetime.strptime(parts[1], "%Y%m%d").date(), parts[2], parts[3]


@dataclass(frozen=True)
class NudgeAnswer:
    status: str  # 'logged' | 'skipped' | 'off' | 'already' | 'unknown'
    report_id: int | None = None
    description: str | None = None


def answer_nudge(
    db_path: Path | str,
    user_id: int,
    day: date,
    name: str,
    answer: str,
    now: datetime | None = None,
) -> NudgeAnswer:
    """Act on a tap under a nudge: store the crowd report, skip, or stop asking.

    A level (1-5) becomes a crowd report for the train the nudge asked about, matched to
    it with `match_method = 'nudge'`. Each nudge can be answered once.
    """
    now = now or datetime.now(UTC)
    with connect(db_path) as con:
        row = con.execute(
            "SELECT train_id, station_code, description, answered FROM bot_notifications "
            "WHERE user_id = ? AND kind = ? AND ref = ? AND day = ?",
            [user_id, NUDGE, name, day],
        ).fetchone()
        if row is None:
            return NudgeAnswer("unknown")
        train_id, station_code, description, answered = row
        if answered:
            return NudgeAnswer("already", description=description)
        con.execute(
            "UPDATE bot_notifications SET answered = true "
            "WHERE user_id = ? AND kind = ? AND ref = ? AND day = ?",
            [user_id, NUDGE, name, day],
        )
        if answer == OFF:
            con.execute(
                "UPDATE favourite_routes SET nudge = false WHERE user_id = ? AND name = ?",
                [user_id, name],
            )
            return NudgeAnswer("off", description=description)
        if answer == SKIP:
            return NudgeAnswer("skipped", description=description)
        (report_id,) = con.execute(
            "INSERT INTO crowd_reports "
            "(reported_at, station_code, train_description, crowd_level, source, note) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            [
                now,
                station_code,
                description,
                int(answer),
                storage.source_for(user_id),
                f"nudge: {name}",
            ],
        ).fetchone()
        matching.store_match(con, report_id, train_id, 1.0, matching.NUDGE, now)
        return NudgeAnswer("logged", report_id, description)
