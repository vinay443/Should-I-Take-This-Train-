"""The collector health report, gap detector and alerts. Fixture databases only, no network."""

from datetime import UTC, date, datetime, timedelta

import duckdb
import httpx
import pytest

from sitt import health, notify
from sitt.config import HealthSettings
from sitt.db import DatabaseBusyError, init_db, open_with_retry
from sitt.ingest.live import runlog
from sitt.tz import IST

SETTINGS = HealthSettings()
TOKEN = "123456:TEST-TOKEN-not-real"


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


def add_run(con, when: datetime, ntes: str = "ok", readings: int = 90, error: str | None = None):
    """One collector run: NTES with the given status, Mobond switched off."""
    run_id = f"{when.astimezone(UTC):%Y%m%dT%H%M%SZ}-test00"
    records = [
        runlog.RunRecord(run_id, "mobond", when, when, "skipped_disabled", host="test"),
        runlog.RunRecord(
            run_id,
            "ntes",
            when,
            when + timedelta(seconds=5),
            ntes,
            readings if ntes != "failed" else 0,
            error=error,
            host="test",
        ),
    ]
    runlog.write_records(con, records)
    if ntes == "ok":
        con.executemany(
            "INSERT INTO observations (observed_at, train_id, station_code, source, "
            "train_number, event, batch_id, delay_minutes) VALUES (?, ?, 'KYN', 'ntes', ?, "
            "'arrival', ?, 2)",
            [[when, "central-95401", "95401", run_id], [when, "12127", "12127", run_id]],
        )


def add_runs(con, start: datetime, end: datetime, minutes: int = 15, **kwargs) -> None:
    when = start
    while when <= end:
        add_run(con, when, **kwargs)
        when += timedelta(minutes=minutes)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "sitt.duckdb"
    init_db(path).close()
    return path


# --- gap detection ---


def test_find_gaps_ignores_ordinary_lateness():
    start = ist(5, 8)
    times = [start + timedelta(minutes=m) for m in (0, 15, 33, 45, 62)]
    assert health.find_gaps(times, start, start + timedelta(minutes=70), 15, 10) == []


def test_find_gaps_counts_missed_runs_between_and_at_the_edges():
    start, end = ist(5, 8), ist(5, 12)
    times = [ist(5, 9), ist(5, 9, 15), ist(5, 10, 15), ist(5, 10, 30)]
    gaps = health.find_gaps(times, start, end, 15, 10)
    assert [(g.start, g.end, g.missed_runs) for g in gaps] == [
        (start, ist(5, 9), 4),  # nothing in the first hour
        (ist(5, 9, 15), ist(5, 10, 15), 3),  # 09:30, 09:45 and 10:00 are missing
        (ist(5, 10, 30), end, 6),  # nothing after 10:30
    ]


def test_clean_runs_have_no_gaps(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 6), ist(6, 6))
        report = health.build_report(con, ist(5, 6), ist(6, 6), SETTINGS, now=ist(6, 6))
    assert report.gaps == [] and report.missed_runs == 0
    assert (report.actual_runs, report.expected_runs) == (97, 96)
    ntes = next(s for s in report.sources if s.source == "ntes")
    assert (ntes.state, ntes.ok, ntes.failed, ntes.readings) == ("up", 97, 0, 97 * 90)
    assert (ntes.trains_matched, ntes.trains_unmatched) == (1, 1)
    mobond = next(s for s in report.sources if s.source == "mobond")
    assert mobond.state == "disabled" and mobond.skipped == 97
    assert report.newest_observation_age == timedelta(0)
    text = health.format_report(report)
    assert "Gaps: none" in text and "mobond: DISABLED" in text
    assert "05 Oct 06:00 to 06 Oct 06:00 IST (24 h)" in text


def test_a_laptop_asleep_overnight_is_one_gap_across_midnight(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 18), ist(5, 23, 30))
        add_runs(con, ist(6, 7), ist(6, 9))
        report = health.build_report(con, ist(5, 18), ist(6, 9), SETTINGS, now=ist(6, 9))
    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert (gap.start, gap.end, gap.missed_runs) == (ist(5, 23, 30), ist(6, 7), 29)
    # A gap is not a source failure: NTES answered every run that happened.
    ntes = next(s for s in report.sources if s.source == "ntes")
    assert ntes.failed == 0 and ntes.outages == [] and ntes.state == "up"
    text = health.format_report(report)
    assert "05 Oct 23:30 to 06 Oct 07:00: 7 h 30 min, about 29 run(s)" in text
    assert "machine off or asleep" in text


def test_a_source_outage_is_not_a_gap(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
        add_runs(con, ist(5, 9, 15), ist(5, 10), ntes="failed", error="POST q returned HTTP 503")
        add_runs(con, ist(5, 10, 15), ist(5, 11))
        report = health.build_report(con, ist(5, 8), ist(5, 11), SETTINGS, now=ist(5, 11))
    assert report.gaps == []
    ntes = next(s for s in report.sources if s.source == "ntes")
    assert (ntes.ok, ntes.failed, ntes.state) == (9, 4, "up")
    assert [(o.start, o.end, o.runs) for o in ntes.outages] == [(ist(5, 9, 15), ist(5, 10), 4)]
    text = health.format_report(report)
    assert "failed 05 Oct 09:15 to 10:00 (4 run(s)): POST q returned HTTP 503" in text


def test_a_source_that_is_still_failing_is_down(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
        add_runs(con, ist(5, 9, 15), ist(5, 10), ntes="failed", error="timed out")
        report = health.build_report(con, ist(5, 8), ist(5, 10), SETTINGS, now=ist(5, 10))
    assert next(s for s in report.sources if s.source == "ntes").state == "down"
    assert "ntes: DOWN" in health.format_summary(report)


def test_time_before_the_first_ever_run_is_not_a_gap(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 15), ist(5, 18))
        report = health.build_report(con, ist(4, 18), ist(5, 18), SETTINGS, now=ist(5, 18))
    assert report.gaps == [] and report.expected_runs == 12


def test_a_calendar_day_window_splits_at_mumbai_midnight(db):
    # 23:50 IST on the 5th is 18:20 UTC on the 5th; 00:05 IST on the 6th is 18:35 UTC on the 5th.
    assert health.window_for(ist(7, 12), day=date(2026, 10, 5)) == (ist(5, 0), ist(6, 0))
    assert health.window_for(ist(5, 12), day=date(2026, 10, 5)) == (ist(5, 0), ist(5, 12))
    with init_db(db) as con:
        add_run(con, ist(5, 23, 50))
        add_run(con, ist(6, 0, 5))
        fifth = health.build_report(con, *health.window_for(ist(7, 12), day=date(2026, 10, 5)))
        sixth = health.build_report(con, *health.window_for(ist(7, 12), day=date(2026, 10, 6)))
    assert (fifth.actual_runs, sixth.actual_runs) == (1, 1)


def test_a_database_without_a_run_log_still_reports(tmp_path):
    path = tmp_path / "old.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE unrelated (x INTEGER)")
    with duckdb.connect(str(path), read_only=True) as con:
        report = health.build_report(con, ist(5, 0), ist(6, 0), SETTINGS, now=ist(6, 0))
        assert health.evaluate_alerts(con, ist(6, 0), SETTINGS)[0].key == "no_successful_run"
        assert health.load_alert_state(con) == {}
    assert not report.has_run_log and "no run log yet" in health.format_report(report)


def test_the_data_quality_hook_plugs_into_both_formats(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
        report = health.build_report(
            con, ist(5, 8), ist(5, 9), SETTINGS, ist(5, 9), lambda con, start, end: ["3 flagged"]
        )
    assert "Data quality:\n  3 flagged" in health.format_report(report)
    assert "DQ: 3 flagged" in health.format_summary(report)


# --- alerts ---


def test_no_alerts_when_all_is_well(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 8), ist(5, 12))
        assert health.evaluate_alerts(con, ist(5, 12, 5), SETTINGS) == []


def test_alert_when_no_run_has_succeeded_for_hours(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
        assert health.evaluate_alerts(con, ist(5, 11, 59), SETTINGS) == []
        (alert,) = health.evaluate_alerts(con, ist(5, 12, 30), SETTINGS)
    assert alert.key == "no_successful_run"
    assert "3 h 30 min" in alert.message and "05 Oct 09:00 IST" in alert.message


def test_alert_when_a_source_is_down_for_n_runs(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
        add_runs(con, ist(5, 9, 15), ist(5, 9, 45), ntes="failed", error="HTTP 503")
        assert health.evaluate_alerts(con, ist(5, 9, 50), SETTINGS) == []  # only 3 so far
        add_run(con, ist(5, 10), ntes="failed", error="HTTP 503")
        (alert,) = health.evaluate_alerts(con, ist(5, 10, 5), SETTINGS)
    assert alert.key == "source_down:ntes"
    assert "last 4 runs, since 05 Oct 09:15 IST" in alert.message and "HTTP 503" in alert.message


def test_alert_when_readings_drop(db):
    with init_db(db) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
        add_runs(con, ist(5, 9, 15), ist(5, 10), readings=3)
        (alert,) = health.evaluate_alerts(con, ist(5, 10, 5), SETTINGS)
        quiet = HealthSettings(alert_min_readings=2)
        assert health.evaluate_alerts(con, ist(5, 10, 5), quiet) == []
    assert alert.key == "low_readings:ntes" and "averaged 3.0 readings" in alert.message


def test_alerts_are_deduplicated_repeated_and_recovered():
    alert = health.Alert("source_down:ntes", "ntes has failed")
    first = health.decide_alerts([alert], {}, ist(5, 10), SETTINGS)
    assert first.messages == ["ALERT: ntes has failed"]
    state = {row[0]: row[1:] for row in first.state_rows}

    # An hour later it is still down: nothing is sent, nothing changes.
    again = health.decide_alerts([alert], state, ist(5, 11), SETTINGS)
    assert again.messages == [] and again.state_rows == []

    # After the repeat interval it is mentioned once more.
    later = health.decide_alerts([alert], state, ist(5, 22), SETTINGS)
    assert later.messages[0].startswith("STILL: ntes has failed (first reported 05 Oct 10:00")
    assert later.state_rows[0][2] == ist(5, 10)  # first_seen_at is kept

    # When it clears, one recovery message, and the state goes inactive.
    cleared = health.decide_alerts([], state, ist(5, 12), SETTINGS)
    assert cleared.messages == ["RECOVERED: source down ntes is fine again."]
    assert cleared.state_rows[0][:2] == ("source_down:ntes", False)
    inactive = {row[0]: row[1:] for row in cleared.state_rows}
    assert health.decide_alerts([], inactive, ist(5, 13), SETTINGS).messages == []


# --- the CLI and Telegram delivery ---


class FakeTelegram:
    def __init__(self, status: int = 200):
        self.status = status
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        import json

        self.sent.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"ok": False, "description": "Forbidden"})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def environment(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("SITT_DB_PATH", str(db))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", "111,222")
    monkeypatch.delenv("SITT_TELEGRAM_SEND", raising=False)
    return db


def test_cli_prints_a_report(environment, capsys):
    with init_db(environment) as con:
        add_runs(con, ist(5, 8), ist(5, 12))
    assert health.main(["--hours", "4"], now=ist(5, 12)) == 0
    out = capsys.readouterr().out
    assert "Collector health, 05 Oct 08:00 to 12:00 IST (4 h)" in out
    assert "Runs: 17 recorded, about 16 expected" in out
    assert "(0 min ago)" in out


def test_telegram_summary_is_a_dry_run_unless_sending_is_switched_on(environment, capsys):
    with init_db(environment) as con:
        add_runs(con, ist(5, 8), ist(5, 12))
    fake = FakeTelegram()
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        assert health.main(["--hours", "4", "--telegram"], now=ist(5, 12), client=client) == 0
    assert fake.sent == []
    assert "[dry run] would send to 2 Telegram user(s):" in capsys.readouterr().out


def test_telegram_summary_is_sent_to_every_allowed_user(environment, monkeypatch, capsys):
    monkeypatch.setenv("SITT_TELEGRAM_SEND", "true")
    with init_db(environment) as con:
        add_runs(con, ist(5, 8), ist(5, 12))
    fake = FakeTelegram()
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        assert health.main(["--hours", "4", "--telegram"], now=ist(5, 12), client=client) == 0
        # --dry-run wins over the switch.
        assert health.main(["--telegram", "--dry-run"], now=ist(5, 12), client=client) == 0
    assert [message["chat_id"] for message in fake.sent] == [111, 222]
    assert fake.sent[0]["text"].startswith("SITT collector, 05 Oct 08:00 to 12:00 IST")
    assert "Sent to 2 Telegram user(s)." in capsys.readouterr().out


def test_alert_only_says_nothing_when_healthy(environment, monkeypatch, capsys):
    monkeypatch.setenv("SITT_TELEGRAM_SEND", "true")
    with init_db(environment) as con:
        add_runs(con, ist(5, 8), ist(5, 12))
    fake = FakeTelegram()
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        assert health.main(["--alert-only"], now=ist(5, 12, 5), client=client) == 0
    assert fake.sent == [] and "Nothing to report" in capsys.readouterr().out


def test_alert_only_sends_once_then_stays_quiet_then_reports_recovery(
    environment, monkeypatch, capsys
):
    monkeypatch.setenv("SITT_TELEGRAM_SEND", "true")
    with init_db(environment) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
    fake = FakeTelegram()
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        assert health.main(["--alert-only"], now=ist(5, 13), client=client) == 0
        assert len(fake.sent) == 2  # one message to each of the two users
        assert "ALERT: No successful collector run for 4 h" in fake.sent[0]["text"]

        assert health.main(["--alert-only"], now=ist(5, 14), client=client) == 0
        assert len(fake.sent) == 2  # deduplicated: still the same problem

        with init_db(environment) as con:
            add_run(con, ist(5, 14, 15))
        assert health.main(["--alert-only"], now=ist(5, 14, 20), client=client) == 0
        assert "RECOVERED: no successful run is fine again." in fake.sent[-1]["text"]
    with duckdb.connect(str(environment), read_only=True) as con:
        assert con.execute("SELECT alert_key, active FROM alert_state").fetchall() == [
            ("no_successful_run", False)
        ]


def test_a_dry_run_alert_stores_no_state(environment, capsys):
    with init_db(environment) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
    assert health.main(["--alert-only"], now=ist(5, 13)) == 0
    assert "[dry run]" in capsys.readouterr().out
    with duckdb.connect(str(environment), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM alert_state").fetchone() == (0,)


def test_a_failed_send_never_leaks_the_token_and_keeps_the_alert_pending(
    environment, monkeypatch, capsys
):
    monkeypatch.setenv("SITT_TELEGRAM_SEND", "true")
    with init_db(environment) as con:
        add_runs(con, ist(5, 8), ist(5, 9))
    with httpx.Client(transport=httpx.MockTransport(FakeTelegram(status=403))) as client:
        assert health.main(["--alert-only"], now=ist(5, 13), client=client) == 3
    captured = capsys.readouterr()
    assert "Could not send" in captured.err and "HTTP 403 (Forbidden)" in captured.err
    assert TOKEN not in captured.err + captured.out
    with duckdb.connect(str(environment), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM alert_state").fetchone() == (0,)


def test_a_connection_error_does_not_leak_the_token():
    def refuse(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with (
        httpx.Client(transport=httpx.MockTransport(refuse)) as client,
        pytest.raises(notify.NotifyError) as error,
    ):
        notify.send_to_users(TOKEN, {1}, "hello", client)
    assert TOKEN not in str(error.value) and "ConnectError" in str(error.value)


def test_sending_needs_a_token_and_users(environment, monkeypatch, capsys):
    monkeypatch.setenv("SITT_TELEGRAM_SEND", "true")
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    assert health.main(["--telegram"], now=ist(5, 12)) == 3
    assert "ALLOWED_USER_IDS must both be set" in capsys.readouterr().err


def test_long_messages_are_clipped():
    assert len(notify.clip("x" * 5000)) <= notify.MESSAGE_LIMIT


# --- a busy or missing database is a message, never a stack trace ---


def test_open_with_retry_waits_then_gives_up_cleanly(db, monkeypatch):
    waits = []

    def busy(*args, **kwargs):
        raise duckdb.IOException("The process cannot access the file")

    monkeypatch.setattr(duckdb, "connect", busy)
    with pytest.raises(DatabaseBusyError, match="in use by another process"):
        open_with_retry(db, read_only=True, attempts=3, wait_seconds=1, sleep=waits.append)
    assert waits == [1, 1.5]


def test_cli_reports_a_busy_database_without_a_traceback(environment, monkeypatch, capsys):
    def busy(*args, **kwargs):
        raise DatabaseBusyError("data/sitt.duckdb is in use by another process")

    monkeypatch.setattr(health, "open_with_retry", busy)
    assert health.main([]) == 2
    assert "in use by another process" in capsys.readouterr().err


def test_cli_reports_a_missing_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert health.main(["--db", str(tmp_path / "nope.duckdb")]) == 2
    assert "No database at" in capsys.readouterr().err
