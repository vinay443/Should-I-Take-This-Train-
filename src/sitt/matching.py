"""Work out which scheduled train a crowd report is about.

A report says "the 8:12 fast from Kalyan was packed". To learn anything about a
particular train, that has to become a `trains.train_id`. This module does it, and
prefers leaving a report unmatched to matching it to the wrong train.

How a match is made
-------------------
1. **When.** The reported time is a clock time. It is taken as the most recent such
   time no later than `future_minutes` after the report was logged, so a report typed
   at 00:10 about the 23:50 belongs to yesterday, and one typed on the platform a few
   minutes before the train belongs to today.
2. **Which trains ran.** Trains that call at the station that day and go on from it
   (a train ending there can't be boarded). Running days follow the day the train
   starts its run; a holiday in `sitt.holidays` runs the Sunday timetable.
3. **What the rider said** rules trains out: fast or slow, the destination ("to CSMT":
   the train must call there afterwards), AC or non-AC, 12 or 15 cars. A ladies'
   special is a candidate only if the report says "ladies special", or the rider can
   board them (`ladies_special_ok`).
4. **Score.** Each remaining train scores `1 - minutes off / window_minutes`: 1 for the
   exact minute, 0 at the edge of the window.
5. **Confidence** is the best score minus half the second-best. One train three
   minutes off scores 0.7. An exact match with another train four minutes away also
   scores 0.7. Two trains two minutes either side score 0.4 each way: too close to call.
6. Below `min_confidence` the report is left unmatched.

Thresholds are `MatchSettings` in sitt.config. All times are Mumbai local time.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb
from dotenv import find_dotenv, load_dotenv

from sitt.bot.parsing import parse_log_text
from sitt.bot.stations import StationDirectory, directory_from
from sitt.config import MatchSettings, load_match_settings, load_settings
from sitt.db import DatabaseBusyError, open_with_retry
from sitt.holidays import is_holiday, is_sunday_schedule
from sitt.tz import IST

AUTO, BACKFILL, USER, USER_NONE, NUDGE = "auto", "backfill", "user", "user_none", "nudge"
# Matches a person made, which nothing automatic may overwrite.
PROTECTED_METHODS = (USER, USER_NONE, NUDGE)

_DESCRIPTION_RE = re.compile(r"(\d{1,2}):(\d{2}) (fast|slow) from (\S+)")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class ReportQuery:
    """What a report says about its train."""

    station_code: str
    departure_time: time
    reported_at: datetime  # timezone-aware
    service: str | None = None  # 'fast' | 'slow'
    destination_code: str | None = None
    is_ac: bool | None = None
    car_count: int | None = None  # 12 or 15
    ladies: bool | None = None


@dataclass(frozen=True)
class Candidate:
    """A train that could be the one reported."""

    train_id: str
    number: str | None
    label: str
    train_type: str
    direction: str
    departure: datetime  # naive Mumbai time, at the report's station
    runs_ac: bool
    car_count: int | None
    is_ladies_special: bool
    minutes_off: float
    score: float

    def describe(self) -> str:
        """e.g. '08:12 fast to CSMT · AC'."""
        tags = []
        if self.is_ladies_special:
            tags.append("ladies special")
        if self.runs_ac:
            tags.append("AC")
        if self.car_count == 15:
            tags.append("15-car")
        text = f"{self.departure:%H:%M} {self.train_type} to {self.label}"
        return " · ".join([text, *tags])


@dataclass(frozen=True)
class MatchResult:
    train: Candidate | None  # the match, when there is a confident one
    confidence: float
    candidates: list[Candidate]  # trains near the reported time, likeliest first
    reason: str  # why, in a few words

    @property
    def matched(self) -> bool:
        return self.train is not None


def reported_departure(query: ReportQuery, settings: MatchSettings) -> datetime:
    """The naive Mumbai datetime the report's clock time refers to (step 1 above)."""
    logged = query.reported_at.astimezone(IST).replace(tzinfo=None)
    latest = logged + timedelta(minutes=settings.future_minutes)
    for offset in (1, 0, -1):
        moment = datetime.combine(logged.date() + timedelta(days=offset), query.departure_time)
        if moment <= latest:
            return moment
    raise AssertionError("unreachable")


def _day_index(day: date) -> int:
    """Position in a Monday-to-Sunday running-days mask. Holidays run as Sundays."""
    return 6 if is_sunday_schedule(day) else day.weekday()


def _runs_ac(is_ac: bool | None, weekdays_only: bool | None, day: date) -> bool:
    if not is_ac:
        return False
    return not (weekdays_only and (day.weekday() >= 5 or is_holiday(day)))


def find_candidates(
    con: duckdb.DuckDBPyConnection,
    query: ReportQuery,
    settings: MatchSettings | None = None,
    ladies_special_ok: bool = False,
) -> list[Candidate]:
    """Trains near the reported time that fit what the rider said, likeliest first."""
    settings = settings or MatchSettings()
    wanted = reported_departure(query, settings)
    rows = con.execute(
        """
        WITH stops AS (
            SELECT *,
                   first_value(coalesce(scheduled_departure, scheduled_arrival))
                       OVER (PARTITION BY train_id ORDER BY stop_seq) AS run_start,
                   max(stop_seq) OVER (PARTITION BY train_id) AS last_seq
            FROM scheduled_stops
        )
        SELECT t.train_id, t.number, t.label, t.train_type, t.direction, t.is_ac,
               t.ac_weekdays_only, t.car_count, t.is_ladies_special,
               s.run_start, coalesce(s.scheduled_departure, s.scheduled_arrival),
               s.days_of_operation
        FROM stops s JOIN trains t USING (train_id)
        WHERE s.station_code = $station AND s.stop_seq < s.last_seq
          AND ($service IS NULL OR t.train_type = $service)
          AND ($destination IS NULL OR EXISTS (
                SELECT 1 FROM scheduled_stops d
                WHERE d.train_id = s.train_id AND d.station_code = $destination
                  AND d.stop_seq > s.stop_seq))
        """,
        {
            "station": query.station_code,
            "service": query.service,
            "destination": query.destination_code,
        },
    ).fetchall()

    reach = max(settings.choice_window_minutes, settings.window_minutes)
    candidates = []
    for row in rows:
        train_id, number, label, train_type, direction, is_ac, weekdays_only = row[:7]
        car_count, is_ladies, run_start, stop_time, days = row[7:]
        if is_ladies and not (query.ladies or ladies_special_ok):
            continue
        if query.ladies and not is_ladies:
            continue
        if query.car_count == 15 and car_count != 15:
            continue
        if query.car_count == 12 and car_count == 15:
            continue
        for offset in (-1, 0, 1):
            service_day = wanted.date() + timedelta(days=offset)
            # Times earlier than the run's start are after midnight (see sitt.timetable).
            calendar_day = service_day + timedelta(days=1 if stop_time < run_start else 0)
            departure = datetime.combine(calendar_day, stop_time)
            minutes_off = abs((departure - wanted).total_seconds()) / 60
            if minutes_off > reach or days[_day_index(service_day)] != "Y":
                continue
            runs_ac = _runs_ac(is_ac, weekdays_only, service_day)
            if query.is_ac is not None and query.is_ac != runs_ac:
                continue
            candidates.append(
                Candidate(
                    train_id=train_id,
                    number=number,
                    label=label,
                    train_type=train_type,
                    direction=direction,
                    departure=departure,
                    runs_ac=runs_ac,
                    car_count=car_count,
                    is_ladies_special=bool(is_ladies),
                    minutes_off=minutes_off,
                    score=max(0.0, 1 - minutes_off / settings.window_minutes),
                )
            )
    candidates.sort(key=lambda c: (-c.score, c.minutes_off, c.departure, c.train_id))
    return candidates


def match(
    con: duckdb.DuckDBPyConnection,
    query: ReportQuery,
    settings: MatchSettings | None = None,
    ladies_special_ok: bool = False,
) -> MatchResult:
    """The scheduled train a report is most likely about, if one stands out."""
    settings = settings or MatchSettings()
    candidates = find_candidates(con, query, settings, ladies_special_ok)
    shown = candidates[: settings.choices]
    if not candidates:
        return MatchResult(None, 0.0, [], "no train near that time")
    best = candidates[0]
    if best.score <= 0:
        return MatchResult(None, 0.0, shown, "no train close enough to that time")
    runner_up = candidates[1].score if len(candidates) > 1 else 0.0
    confidence = round(best.score - 0.5 * runner_up, 3)
    if confidence < settings.min_confidence:
        reason = "two trains too close to tell apart" if runner_up > 0 else "not close enough"
        return MatchResult(None, confidence, shown, reason)
    return MatchResult(best, confidence, shown, "matched")


# --- stored reports ---


@dataclass(frozen=True)
class StoredMatch:
    report_id: int
    source: str
    query: ReportQuery | None  # None when the report can't be read as "HH:MM type from X"
    matched_train_id: str | None
    match_method: str | None


def query_from_report(
    station_code: str | None,
    train_description: str | None,
    note: str | None,
    reported_at: datetime,
    stations: StationDirectory,
) -> ReportQuery | None:
    """Rebuild what a stored report says about its train.

    The time, type and station come from `train_description`; the optional details (to
    where, AC, cars, ladies' special) from the raw message kept in `note`.
    """
    found = _DESCRIPTION_RE.fullmatch((train_description or "").strip())
    if found is None:
        return None
    extras = parse_log_text(re.sub(r"^/\w+(@\w+)?", "", note or ""), stations)
    return ReportQuery(
        station_code=station_code or found[4],
        departure_time=time(int(found[1]), int(found[2])),
        reported_at=reported_at,
        service=found[3],
        destination_code=extras.destination_code,
        is_ac=extras.is_ac,
        car_count=extras.car_count,
        ladies=extras.ladies,
    )


def load_report(con: duckdb.DuckDBPyConnection, report_id: int) -> StoredMatch | None:
    row = con.execute(
        "SELECT id, source, station_code, train_description, note, epoch_us(reported_at), "
        "matched_train_id, match_method FROM crowd_reports WHERE id = ?",
        [report_id],
    ).fetchone()
    if row is None:
        return None
    reported_at = _EPOCH + timedelta(microseconds=row[5])
    query = query_from_report(row[2], row[3], row[4], reported_at, directory_from(con))
    return StoredMatch(row[0], row[1], query, row[6], row[7])


def store_match(
    con: duckdb.DuckDBPyConnection,
    report_id: int,
    train_id: str | None,
    confidence: float | None,
    method: str,
    now: datetime | None = None,
) -> None:
    con.execute(
        "UPDATE crowd_reports SET matched_train_id = ?, match_confidence = ?, match_method = ?, "
        "matched_at = ? WHERE id = ?",
        [train_id, confidence if train_id else None, method, now or datetime.now(UTC), report_id],
    )


def match_stored(
    con: duckdb.DuckDBPyConnection,
    report_id: int,
    method: str = AUTO,
    settings: MatchSettings | None = None,
    ladies_special_ok: bool = False,
    now: datetime | None = None,
) -> MatchResult | None:
    """Match one stored report and record the outcome. None if it can't be read or is protected."""
    stored = load_report(con, report_id)
    if stored is None or stored.query is None or stored.match_method in PROTECTED_METHODS:
        return None
    result = match(con, stored.query, settings, ladies_special_ok)
    store_match(
        con,
        report_id,
        result.train.train_id if result.train else None,
        result.confidence,
        method,
        now,
    )
    return result


def backfill(
    con: duckdb.DuckDBPyConnection,
    settings: MatchSettings | None = None,
    ladies_special_ok: bool = False,
    rematch: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> list[tuple[int, str, MatchResult | None]]:
    """Match reports that haven't been tried yet (or, with `rematch`, every automatic match).

    Matches a person made ('user', 'user_none', 'nudge') are never touched. Returns
    (report id, description, result) per report considered; the result is None when the
    report's description can't be read.
    """
    methods = "match_method IS NULL" + (
        f" OR match_method IN ('{AUTO}', '{BACKFILL}')" if rematch else ""
    )
    rows = con.execute(
        f"SELECT id, train_description FROM crowd_reports WHERE {methods} ORDER BY id"
    ).fetchall()
    out = []
    for report_id, description in rows:
        stored = load_report(con, report_id)
        if stored is None or stored.query is None:
            out.append((report_id, description or "", None))
            continue
        result = match(con, stored.query, settings, ladies_special_ok)
        if not dry_run:
            train_id = result.train.train_id if result.train else None
            store_match(con, report_id, train_id, result.confidence, BACKFILL, now)
        out.append((report_id, description or "", result))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sitt-match-logs",
        description="Match crowd reports logged with the bot to scheduled trains. Reports "
        "the rider matched by hand are never changed.",
        epilog="Thresholds are set with SITT_MATCH_* variables. See docs/crowding.md.",
    )
    parser.add_argument("--db", type=Path, help="default: SITT_DB_PATH or data/sitt.duckdb")
    parser.add_argument(
        "--rematch",
        action="store_true",
        help="also redo reports matched automatically before (e.g. after a new timetable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the matches, store nothing")
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    try:
        settings = load_settings()
        match_settings = load_match_settings()
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    db_path = args.db or settings.db_path
    try:
        with open_with_retry(db_path, read_only=args.dry_run) as con:
            results = backfill(
                con,
                match_settings,
                settings.recommend.ladies_special_ok,
                rematch=args.rematch,
                dry_run=args.dry_run,
            )
    except FileNotFoundError as exc:
        print(f"{exc}. Run `uv run sitt-init-db` first.", file=sys.stderr)
        return 2
    except DatabaseBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    matched = 0
    for report_id, description, result in results:
        if result is None:
            print(f"#{report_id} {description!r}: can't be read as a time, type and station")
        elif result.train is None:
            print(f"#{report_id} {description}: left unmatched ({result.reason})")
        else:
            matched += 1
            print(
                f"#{report_id} {description}: {result.train.describe()} "
                f"[{result.train.train_id}, confidence {result.confidence:.2f}]"
            )
    suffix = " (dry run, nothing stored)" if args.dry_run else ""
    print(f"{matched} of {len(results)} report(s) matched{suffix}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
