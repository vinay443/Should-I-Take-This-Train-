"""Reading and writing bot reports in `crowd_reports`."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sitt.db import connect

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def source_for(telegram_user_id: int) -> str:
    """`crowd_reports.source` for a bot report; it doubles as the reporter's identity."""
    return f"telegram:{telegram_user_id}"


@dataclass(frozen=True)
class NewReport:
    telegram_user_id: int
    reported_at: datetime  # timezone-aware
    station_code: str
    train_description: str  # e.g. "08:12 fast from KYN", resolved to a train_id later
    crowd_level: int
    note: str | None  # raw message text


@dataclass(frozen=True)
class StoredReport:
    id: int
    reported_at: datetime  # UTC
    station_code: str | None
    train_description: str | None
    crowd_level: int


def insert_report(db_path: Path | str, report: NewReport) -> int:
    """Insert a report and return its id. The schema must already exist (see `init_db`)."""
    with connect(db_path) as con:
        (report_id,) = con.execute(
            "INSERT INTO crowd_reports "
            "(reported_at, station_code, train_description, crowd_level, source, note) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            [
                report.reported_at,
                report.station_code,
                report.train_description,
                report.crowd_level,
                source_for(report.telegram_user_id),
                report.note,
            ],
        ).fetchone()
    return report_id


def recent_reports(
    db_path: Path | str, telegram_user_id: int, limit: int = 10
) -> list[StoredReport]:
    """The user's most recent reports, newest first."""
    with connect(db_path) as con:
        # epoch_us avoids DuckDB needing pytz to hand back TIMESTAMPTZ values.
        rows = con.execute(
            "SELECT id, epoch_us(reported_at), station_code, train_description, crowd_level "
            "FROM crowd_reports WHERE source = ? ORDER BY reported_at DESC, id DESC LIMIT ?",
            [source_for(telegram_user_id), limit],
        ).fetchall()
    return [
        StoredReport(
            id=report_id,
            reported_at=_EPOCH + timedelta(microseconds=micros),
            station_code=station_code,
            train_description=train_description,
            crowd_level=crowd_level,
        )
        for report_id, micros, station_code, train_description, crowd_level in rows
    ]
