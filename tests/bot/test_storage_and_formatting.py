from datetime import UTC, datetime, time

import pytest

from sitt.bot import storage
from sitt.bot.flow import LogDraft
from sitt.bot.formatting import describe_draft, format_report_list
from sitt.db import init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.duckdb"
    init_db(path).close()
    return path


def _report(user_id: int, minute: int, level: int = 4) -> storage.NewReport:
    return storage.NewReport(
        telegram_user_id=user_id,
        reported_at=datetime(2026, 9, 27, 3, minute, tzinfo=UTC),
        station_code="KYN",
        train_description="08:12 fast from KYN",
        crowd_level=level,
        note="/log 8:12 fast KYN packed",
    )


def test_insert_and_read_back_newest_first(db_path):
    first = storage.insert_report(db_path, _report(1, minute=0))
    second = storage.insert_report(db_path, _report(1, minute=5, level=2))
    storage.insert_report(db_path, _report(2, minute=10))  # someone else's

    reports = storage.recent_reports(db_path, telegram_user_id=1)
    assert [r.id for r in reports] == [second, first]
    assert reports[1] == storage.StoredReport(
        id=first,
        reported_at=datetime(2026, 9, 27, 3, 0, tzinfo=UTC),
        station_code="KYN",
        train_description="08:12 fast from KYN",
        crowd_level=4,
    )


def test_insert_stores_source_and_raw_note(db_path):
    report_id = storage.insert_report(db_path, _report(42, minute=0))
    with init_db(db_path) as con:
        row = con.execute(
            "SELECT source, note FROM crowd_reports WHERE id = ?", [report_id]
        ).fetchone()
    assert row == ("telegram:42", "/log 8:12 fast KYN packed")


def test_recent_reports_limit(db_path):
    for minute in range(12):
        storage.insert_report(db_path, _report(1, minute=minute))
    assert len(storage.recent_reports(db_path, telegram_user_id=1, limit=10)) == 10


def test_format_report_list(db_path):
    storage.insert_report(db_path, _report(1, minute=0))
    text = format_report_list(storage.recent_reports(db_path, telegram_user_id=1))
    assert text == "Your last 1 report:\n• Sun 27 Sep · 08:12 fast from KYN · 4/5 packed"


def test_format_empty_report_list():
    assert "No reports yet" in format_report_list([])


def test_describe_draft():
    assert describe_draft(LogDraft(raw_text="")) == ""
    assert describe_draft(LogDraft(raw_text="", crowd_level=5)) == "5/5 can't board"
    draft = LogDraft(
        raw_text="", station_code="TNA", departure_time=time(18, 5), service="slow", crowd_level=3
    )
    assert describe_draft(draft) == "18:05 slow from Thane (TNA) · 3/5 standing"
