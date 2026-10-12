"""`sitt-next`: the command-line recommender."""

from datetime import date, datetime, time

import pytest
from conftest import SAMPLE_CSV

from sitt import next_cli
from sitt.db import init_db
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
