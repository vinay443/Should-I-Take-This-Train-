"""`sitt-next`: the command-line recommender."""

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb
import httpx
import pytest
from conftest import SAMPLE_CSV

from sitt import next_cli
from sitt.db import init_db
from sitt.ingest.live import mobond, ntes
from sitt.ingest.live.common import make_client
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.tz import IST

TUESDAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 7, 0, tzinfo=IST)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env, no saved model
    monkeypatch.delenv("SITT_DB_PATH", raising=False)
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    return path


def run(db, capsys, *args, now=NOW):
    code = next_cli.main(["--db", str(db), *args], now=now)
    out, err = capsys.readouterr()
    return code, out, err


def test_prints_the_recommendation_and_a_table(db, capsys):
    code, out, err = run(db, capsys, "KYN", "CSMT")
    assert code == 0 and not err
    lines = out.splitlines()
    assert (
        lines[0]
        == "Kalyan -> Chhatrapati Shivaji Maharaj Terminus, asked at 07:00 on Tue 13 Oct 2026"
    )
    # The 07:12 fast overtakes the 07:03 slow, so the rider lets the slow go.
    assert lines[2].startswith("Wait for the 07:12 fast: arrives 08:19")
    header = next(line for line in lines if line.strip().startswith("Leaves"))
    assert "Arrives" in header and "Crowding" in header
    chosen = [line for line in lines if line.startswith(">")]
    assert len(chosen) == 1
    assert "Times: timetable only" in out
    assert "rule-of-thumb" in out


def test_station_names_work_as_well_as_codes(db, capsys):
    code, out, _ = run(db, capsys, "kalyan", "Chhatrapati Shivaji Maharaj Terminus")
    assert code == 0
    assert out.startswith("Kalyan -> Chhatrapati Shivaji Maharaj Terminus")


def test_at_and_date_set_when_the_question_is_asked(db, capsys):
    code, out, _ = run(db, capsys, "KYN", "CSMT", "--date", "2026-10-14", "--at", "07:30")
    assert code == 0
    assert "asked at 07:30 on Wed 14 Oct 2026" in out
    first_row = next(line for line in out.splitlines() if "slow to" in line or "fast to" in line)
    assert "07:31" in first_row


def test_a_date_alone_asks_from_midnight(db, capsys):
    _, out, _ = run(db, capsys, "KYN", "CSMT", "--date", "2026-10-14")
    assert "asked at 00:00 on Wed 14 Oct 2026" in out


def test_train_focuses_on_one_train(db, capsys):
    code, out, _ = run(db, capsys, "KYN", "CSMT", "--train", "07:31")
    assert code == 0
    assert "asked at 07:30" in out
    assert "The 07:31 fast to CSMT (90108):" in out
    assert "leaves 07:31 scheduled" in out
    assert ("This is the recommended train." in out) or ("Recommended instead:" in out)


def test_a_train_that_does_not_exist_is_an_error(db, capsys):
    code, out, err = run(db, capsys, "KYN", "CSMT", "--train", "07:13")
    assert code == 1 and not out
    assert "No train leaves Kalyan at 07:13" in err
    assert "07:12 fast" in err


def test_unknown_station(db, capsys):
    code, out, err = run(db, capsys, "KYN", "Nowhere")
    assert code == 2 and not out
    assert "Nowhere" in err


def test_same_station(db, capsys):
    code, _, err = run(db, capsys, "KYN", "Kalyan")
    assert code == 2
    assert "same station" in err


def test_missing_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "nope.duckdb"
    assert next_cli.main(["--db", str(missing), "KYN", "CSMT"], now=NOW) == 2
    assert "sitt-init-db" in capsys.readouterr().err
    assert not missing.exists()  # it must not create an empty database


@pytest.mark.parametrize("bad", ["25:00", "8", "noon"])
def test_bad_time(db, capsys, bad):
    with pytest.raises(SystemExit) as exc:
        next_cli.main(["--db", str(db), "KYN", "CSMT", "--at", bad], now=NOW)
    assert exc.value.code == 2


def test_why_prints_the_full_explanation(db, capsys):
    code, out, _ = run(db, capsys, "KYN", "CSMT", "--why")
    assert code == 0
    assert "Predictions used: timetable only" in out
    assert "Rules: take the earliest predicted arrival" in out


def test_n_limits_the_trains_compared(db, capsys):
    _, out, _ = run(db, capsys, "KYN", "CSMT", "-n", "2")
    rows = [line for line in out.splitlines() if " to CSMT" in line]
    assert len(rows) == 2


def test_asked_at():
    assert next_cli.asked_at(NOW, None, None, None) == NOW
    assert next_cli.asked_at(NOW, time(9, 0), None, None) == datetime(
        2026, 10, 13, 9, 0, tzinfo=IST
    )
    assert next_cli.asked_at(NOW, None, TUESDAY, time(8, 12)) == datetime(
        2026, 10, 13, 8, 11, tzinfo=IST
    )
    # --at wins over --train
    assert next_cli.asked_at(NOW, time(8, 0), None, time(8, 12)).time() == time(8, 0)


def test_a_typo_gets_a_suggestion(db, capsys):
    code, _, err = run(db, capsys, "KYN", "Thaane")
    assert code == 2
    assert "Did you mean Thane (TNA)?" in err


# --- --collect: one manual collection round, against a fake NTES ---

LIVE_FIXTURES = Path(__file__).parent / "fixtures" / "live"


class FakeNtes:
    def __init__(self, status: int = 200):
        self.status = status
        self.hosts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        url = str(request.url)
        if url == ntes.BASE_URL:
            return httpx.Response(self.status, text="<html>home</html>")
        if url.startswith(f"{ntes.BASE_URL}GetCSRFToken"):
            return httpx.Response(200, text="<input type='hidden' name='tk1' value='v1'>")
        if url.startswith(f"{ntes.BASE_URL}q?"):
            board = (LIVE_FIXTURES / "ntes_live_station_kyn.html").read_text(encoding="utf-8")
            return httpx.Response(200, text=board)
        return httpx.Response(404)


@pytest.fixture
def no_mobond(monkeypatch):
    monkeypatch.setattr(ntes, "PAUSE_SECONDS", 0)
    monkeypatch.delenv(mobond.ENABLE_VARIABLE, raising=False)
    monkeypatch.delenv(mobond.PERMISSION_VARIABLE, raising=False)


def test_collect_fetches_once_then_recommends(db, tmp_path, capsys, no_mobond):
    fake = FakeNtes()
    with make_client(httpx.MockTransport(fake)) as client:
        code = next_cli.main(
            ["--db", str(db), "--data-dir", str(tmp_path / "data"), "--collect", "KYN", "CSMT"],
            now=NOW,
            client=client,
        )
    out = capsys.readouterr().out
    assert code == 0
    first = out.splitlines()[0]
    assert first.startswith("Live check just now: ntes ") and first.endswith(" readings.")
    assert int(first.split("ntes ")[1].split()[0]) > 0
    assert "Kalyan -> Chhatrapati Shivaji Maharaj Terminus" in out
    assert set(fake.hosts) == {"enquiry.indianrail.gov.in"}  # NTES only, never Mobond
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM observations").fetchone()[0] > 0
        assert con.execute("SELECT count(*) FROM collector_runs").fetchone()[0] >= 1


def test_a_failed_collection_still_recommends(db, tmp_path, capsys, no_mobond):
    with make_client(httpx.MockTransport(FakeNtes(status=503))) as client:
        code = next_cli.main(
            ["--db", str(db), "--data-dir", str(tmp_path / "data"), "--collect", "KYN", "CSMT"],
            now=NOW,
            client=client,
        )
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0].startswith("Live check just now: ntes failed (")
    assert "Kalyan -> Chhatrapati Shivaji Maharaj Terminus" in out


def test_without_collect_nothing_is_fetched(db, capsys):
    def refuse(request):
        raise AssertionError("no request may be made without --collect")

    with make_client(httpx.MockTransport(refuse)) as client:
        assert next_cli.main(["--db", str(db), "KYN", "CSMT"], now=NOW, client=client) == 0
    assert "Live check" not in capsys.readouterr().out


# --- live, cancelled and history rows ---


def _observe(db, when: datetime, number: str, delay, source="ntes", cancelled=False):
    with init_db(db) as con:
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
            "source, train_number, event, cancelled) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [when, f"central-{number}", "" if cancelled else "KYN", delay, source, number,
             "cancelled" if cancelled else "at", cancelled],
        )  # fmt: skip


def test_live_cancelled_and_focus_on_a_train_that_is_not_the_pick(db, capsys):
    _observe(db, NOW - timedelta(minutes=3), "90106", 6)  # the 07:12 fast, 6 late
    _observe(db, NOW - timedelta(minutes=20), "90108", None, cancelled=True)
    code, out, _ = run(db, capsys, "KYN", "CSMT", "--train", "07:12", "--at", "07:00")
    assert code == 0
    rows = {
        re.search(r"\d\d:\d\d", line).group(): line
        for line in out.splitlines()
        if " to CSMT" in line and not line.startswith("The ")
    }
    assert "6 min late (live 06:57)" in rows["07:12"]
    assert "CANCELLED" in rows["07:31"]
    assert "Times: timetable, except trains marked live." in out
    assert "live: 6 min late at 06:57, at KYN" in out
    # 07:12 + 6 = 08:25 arrival, after the 07:03 slow's 08:24: so the slow is the pick.
    assert "Recommended instead: the 07:03 slow." in out

    code, out, _ = run(db, capsys, "KYN", "CSMT", "--train", "07:31", "--at", "07:00")
    assert "reported CANCELLED" in out


def test_history_gives_a_range_and_synthetic_is_flagged(db, capsys):
    # Six earlier days of the 07:12 fast: on time at Kalyan, 12-16 min late into CSMT.
    with init_db(db) as con:
        for back, late in enumerate([12, 13, 14, 15, 16, 14], start=1):
            day = NOW - timedelta(days=back)
            for station, delay, hhmm in (("KYN", 0, (7, 12)), ("CSMT", late, (8, 30))):
                con.execute(
                    "INSERT INTO observations (observed_at, train_id, station_code, "
                    "delay_minutes, source, train_number, event) "
                    "VALUES (?, 'central-90106', ?, ?, 'synthetic', '90106', 'at')",
                    [day.replace(hour=hhmm[0], minute=hhmm[1]), station, delay],
                )
    code, out, _ = run(db, capsys, "KYN", "CSMT", "--train", "07:12", "--at", "07:00")
    assert code == 0
    assert "Times: typical past delay of each train, built from SYNTHETIC" in out
    assert "SYNTHETIC: these delays come from invented data" in out
    assert "likely between" in out  # the focus block shows the 10th-90th percentile range
    row = next(
        line
        for line in out.splitlines()
        if " to CSMT" in line and not line.startswith("The ") and "07:12" in line
    )
    assert "±" in row


def test_collect_reports_a_crash_without_stopping(db, tmp_path, capsys, monkeypatch):
    from sitt.ingest.live import local

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(local, "run_once", boom)
    code = next_cli.main(
        ["--db", str(db), "--data-dir", str(tmp_path / "d"), "--collect", "KYN", "CSMT"], now=NOW
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("Live check failed (RuntimeError); using what the database already has.")
