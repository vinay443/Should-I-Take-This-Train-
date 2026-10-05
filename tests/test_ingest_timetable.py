from datetime import time
from textwrap import dedent

import duckdb
import pytest
from conftest import SAMPLE_CSV

from sitt.db import init_db
from sitt.ingest.timetable import (
    TimetableError,
    load_timetable,
    main,
    parse_days,
    read_timetable,
)

HEADER = (
    "train_number,destination,service_type,direction,station_code,station_name,"
    "scheduled_arrival,scheduled_departure,days"
)
SMALL = dedent(
    f"""\
    # Two made-up trains for validation tests.
    {HEADER}
    1,Kalyan,slow,down,TNA,Thane,,08:00,daily
    1,Kalyan,slow,down,DI,Dombivli,08:20,08:21,daily
    1,Kalyan,slow,down,KYN,Kalyan,08:28,,daily
    2,Thane,fast,up,KYN,Kalyan,,09:00,mon-sat
    2,Thane,fast,up,TNA,Thane,09:25,,mon-sat
    """
)


def write(tmp_path, text, name="timetable.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def snapshot(con):
    return {
        table: con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        for table in ("stations", "trains", "scheduled_stops")
    }


def test_load_sample(loaded):
    counts = [
        loaded.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("stations", "trains", "scheduled_stops")
    ]
    assert counts == [26, 13, 246]

    assert loaded.execute("SELECT * FROM trains WHERE train_id = 'central-90105'").fetchone() == (
        "central-90105",
        "90105",
        "Titwala",
        "slow",
        "central",
        "down",
        # service_code, is_ac, car_count, is_ladies_special, ac_weekdays_only:
        # not in the sample CSV
        *(None,) * 5,
    )

    stops = loaded.execute(
        """
        SELECT station_code, stop_seq, scheduled_arrival, scheduled_departure, days_of_operation
        FROM scheduled_stops WHERE train_id = 'central-90106' ORDER BY stop_seq
        """
    ).fetchall()
    assert [s[0] for s in stops] == ["KYN", "DI", "TNA", "MLND", "GC", "CLA", "DR", "BY", "CSMT"]
    assert [s[1] for s in stops] == list(range(1, 10))
    assert stops[0][2:] == (None, time(7, 12), "YYYYYYY")
    assert stops[-1][2:] == (time(8, 19), None, "YYYYYYY")


def test_station_seq_runs_from_csmt(loaded):
    rows = loaded.execute("SELECT code, seq, line FROM stations ORDER BY seq").fetchall()
    assert [r[1] for r in rows] == list(range(1, 27))
    assert rows[0][0] == "CSMT" and rows[18][0] == "TNA" and rows[-1][0] == "KYN"
    assert {r[2] for r in rows} == {"central"}


def test_days_and_midnight_wrap_are_stored(loaded):
    days = dict(
        loaded.execute(
            "SELECT train_id, any_value(days_of_operation) FROM scheduled_stops GROUP BY ALL"
        ).fetchall()
    )
    assert days["central-90105"] == "YYYYYYN"
    assert days["central-90109"] == "NNNNNNY"

    late = loaded.execute(
        """
        SELECT station_code, scheduled_arrival, scheduled_departure FROM scheduled_stops
        WHERE train_id = 'central-90111' AND station_code IN ('BY', 'KYN') ORDER BY stop_seq
        """
    ).fetchall()
    assert late == [("BY", time(23, 59), time(0, 0)), ("KYN", time(1, 13), None)]


def test_reload_is_idempotent(loaded):
    before = snapshot(loaded)
    load_timetable(loaded, read_timetable(SAMPLE_CSV))
    load_timetable(loaded, read_timetable(SAMPLE_CSV), replace=True)
    assert snapshot(loaded) == before


def test_cli_rerun_is_idempotent(tmp_path, capsys):
    db = tmp_path / "sitt.duckdb"
    assert main([str(SAMPLE_CSV), "--db", str(db)]) == 0
    assert "Loaded 13 trains, 246 stops and 26 stations" in capsys.readouterr().out
    with init_db(db) as con:
        first = snapshot(con)
    assert main([str(SAMPLE_CSV), "--db", str(db)]) == 0
    with init_db(db) as con:
        assert snapshot(con) == first


def test_cli_rejects_invalid_file_without_touching_db(tmp_path, capsys):
    db = tmp_path / "sitt.duckdb"
    bad = write(tmp_path, SMALL.replace("08:20,08:21", "08:20,8:5"))
    assert main([str(bad), "--db", str(db)]) == 1
    assert "invalid time '8:5'" in capsys.readouterr().err
    assert not db.exists()


def test_reloading_a_train_replaces_its_stops(con, tmp_path):
    load_timetable(con, read_timetable(write(tmp_path, SMALL)))
    changed = SMALL.replace("1,Kalyan,slow,down,DI,Dombivli,08:20,08:21,daily\n", "")
    changed = changed.replace("08:28", "08:30")
    load_timetable(con, read_timetable(write(tmp_path, changed)))

    assert con.execute(
        "SELECT station_code, stop_seq, scheduled_arrival FROM scheduled_stops "
        "WHERE train_id = 'central-1' ORDER BY stop_seq"
    ).fetchall() == [("TNA", 1, None), ("KYN", 2, time(8, 30))]


def test_trains_missing_from_file_are_kept_unless_replace(con, tmp_path):
    load_timetable(con, read_timetable(SAMPLE_CSV))
    only_small = read_timetable(write(tmp_path, SMALL))

    load_timetable(con, only_small)
    assert con.execute("SELECT count(*) FROM trains").fetchone() == (15,)

    result = load_timetable(con, only_small, replace=True)
    assert result.removed_trains == 13
    assert con.execute("SELECT train_id FROM trains ORDER BY ALL").fetchall() == [
        ("central-1",),
        ("central-2",),
    ]
    assert con.execute("SELECT count(*) FROM scheduled_stops").fetchone() == (5,)

    before = snapshot(con)
    load_timetable(con, only_small, replace=True)
    assert snapshot(con) == before


def test_replace_only_affects_its_own_line(con, tmp_path):
    load_timetable(con, read_timetable(SAMPLE_CSV))
    harbour = SMALL.replace("TNA", "VSH").replace("Thane", "Vashi")
    harbour = harbour.replace("DI,Dombivli", "BEPR,Belapur").replace("KYN,Kalyan", "PNVL,Panvel")
    load_timetable(con, read_timetable(write(tmp_path, harbour), line="harbour"), replace=True)
    assert con.execute(
        "SELECT line, count(*) FROM trains GROUP BY line ORDER BY line"
    ).fetchall() == [("central", 13), ("harbour", 2)]


def test_conflict_with_loaded_trains_rolls_back(loaded, tmp_path):
    before = snapshot(loaded)
    # An "up" train whose stops are listed in the down order.
    wrong_way = dedent(
        f"""\
        {HEADER}
        99,CSMT,fast,up,TNA,Thane,,10:00,daily
        99,CSMT,fast,up,KYN,Kalyan,10:20,,daily
        """
    )
    timetable = read_timetable(write(tmp_path, wrong_way))  # fine on its own
    with pytest.raises(TimetableError, match="already loaded.*disagrees"):
        load_timetable(loaded, timetable)
    assert snapshot(loaded) == before


def test_comment_lines_keep_line_numbers(tmp_path):
    path = write(tmp_path, SMALL.replace("09:25", "09:99"))
    with pytest.raises(TimetableError) as e:
        read_timetable(path)
    assert e.value.errors == [
        "line 7: scheduled_arrival: invalid time '09:99', expected HH:MM between 00:00 and 23:59"
    ]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("08:20,08:21", "08:20,24:00", "invalid time '24:00'"),
        ("08:20,08:21", "08:22,08:21", "departure at DI 08:21 does not follow arrival at DI 08:22"),
        ("08:28", "07:58", "arrival at KYN 07:58 does not follow departure at DI 08:21"),
        ("1,Kalyan,slow,down,DI", "1,Kalyan,fast,down,DI", "service_type 'fast' here but 'slow'"),
        ("1,Kalyan,slow,down,DI", "1,Titwala,slow,down,DI", "destination 'Titwala'"),
        ("2,Thane,fast,up,KYN", "2,Thane,fast,north,KYN", "direction 'north' is not 'up'"),
        ("2,Thane,fast,up,KYN", "2,Thane,express,up,KYN", "service_type 'express' is not"),
        ("DI,Dombivli", "KYN,Kalyan", "train central-1 stops at KYN more than once"),
        ("TNA,Thane,09:25", "TNA,Thane West,09:25", "named 'Thane West' here but 'Thane'"),
        (",,08:00,daily", ",,,daily", "first stop needs a scheduled_departure"),
        ("09:25,,", ",09:25,", "last stop needs a scheduled_arrival"),
        ("08:20,08:21", ",", "needs a scheduled_arrival or scheduled_departure"),
        ("mon-sat\n2,Thane,fast,up,TNA", "weekdays\n2,Thane,fast,up,TNA", "invalid days"),
        ("KYN,Kalyan,08:28,,daily", "KYN,Kalyan,08:28,,daily,extra", "expected 9 fields, got 10"),
        ("2,Thane,fast,up,KYN,Kalyan,,09:00,mon-sat\n", "", "train central-2 has only one stop"),
        ("2,Thane,fast,up", "2,Thane,fast,down", "disagrees with train 1 about station order"),
        (",scheduled_departure,", ",departure,", "missing column 'scheduled_departure'"),
    ],
)
def test_validation_errors(tmp_path, old, new, message):
    assert old in SMALL
    path = write(tmp_path, SMALL.replace(old, new, 1))
    with pytest.raises(TimetableError) as e:
        read_timetable(path)
    assert any(message in err for err in e.value.errors), e.value.errors


def test_header_only_file_is_rejected(tmp_path):
    with pytest.raises(TimetableError, match="no trains in file"):
        read_timetable(write(tmp_path, HEADER + "\n"))


def test_explicit_train_id_keeps_same_number_apart(con, tmp_path):
    text = dedent(
        f"""\
        {HEADER},train_id
        5,Kalyan,slow,down,TNA,Thane,,08:00,mon-sat,5-weekday
        5,Kalyan,slow,down,KYN,Kalyan,08:28,,mon-sat,5-weekday
        5,Kalyan,slow,down,TNA,Thane,,09:00,sun,5-sunday
        5,Kalyan,slow,down,KYN,Kalyan,09:28,,sun,5-sunday
        """
    )
    load_timetable(con, read_timetable(write(tmp_path, text)))
    assert con.execute("SELECT train_id, number FROM trains ORDER BY ALL").fetchall() == [
        ("5-sunday", "5"),
        ("5-weekday", "5"),
    ]


ATTRIBUTES = dedent(
    f"""    {HEADER},service_code,ac,cars,notes
    1,Kalyan,slow,down,TNA,Thane,,08:00,daily,K 1,yes,15,ladies_coaches
    1,Kalyan,slow,down,KYN,Kalyan,08:28,,daily,K 1,yes,15,ladies_coaches
    2,Thane,fast,up,KYN,Kalyan,,09:00,mon-sat,T 2,no,,ladies_special|ladies_coaches
    2,Thane,fast,up,TNA,Thane,09:25,,mon-sat,T 2,no,,ladies_special|ladies_coaches
    3,Thane,slow,up,KYN,Kalyan,,10:00,daily,,,,
    3,Thane,slow,up,TNA,Thane,10:30,,daily,,,,
    """
)
ATTRIBUTE_QUERY = (
    "SELECT number, service_code, is_ac, car_count, is_ladies_special FROM trains ORDER BY number"
)


def test_optional_train_attributes_are_stored(con, tmp_path):
    load_timetable(con, read_timetable(write(tmp_path, ATTRIBUTES)))
    assert con.execute(ATTRIBUTE_QUERY).fetchall() == [
        ("1", "K 1", True, 15, False),
        ("2", "T 2", False, None, True),
        ("3", None, None, None, False),  # blank ac/cars mean "not stated"
    ]


def test_non_ac_weekends_note_is_stored(con, tmp_path):
    text = ATTRIBUTES.replace("K 1,yes,15,ladies_coaches", "K 1,yes,15,non_ac_weekends")
    load_timetable(con, read_timetable(write(tmp_path, text)))
    assert con.execute(
        "SELECT number, is_ac, ac_weekdays_only FROM trains ORDER BY number"
    ).fetchall() == [("1", True, True), ("2", False, False), ("3", None, False)]


def test_reloading_without_attribute_columns_clears_them(con, tmp_path):
    load_timetable(con, read_timetable(write(tmp_path, ATTRIBUTES)))
    load_timetable(con, read_timetable(write(tmp_path, SMALL)))
    assert con.execute(ATTRIBUTE_QUERY).fetchall() == [
        ("1", None, None, None, None),
        ("2", None, None, None, None),
        ("3", None, None, None, False),  # not in SMALL, so left as loaded
    ]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("K 1,yes,15", "K 1,maybe,15", "ac: invalid value 'maybe'"),
        ("K 1,yes,15", "K 1,yes,many", "cars: invalid car count 'many'"),
        ("08:28,,daily,K 1,yes", "08:28,,daily,K 1,no", "is_ac False here but True"),
        ("08:28,,daily,K 1", "08:28,,daily,K 9", "service_code 'K 9' here but 'K 1'"),
    ],
)
def test_attribute_validation_errors(tmp_path, old, new, message):
    assert old in ATTRIBUTES
    with pytest.raises(TimetableError) as e:
        read_timetable(write(tmp_path, ATTRIBUTES.replace(old, new, 1)))
    assert any(message in err for err in e.value.errors), e.value.errors


def test_schema_adds_train_attribute_columns_to_an_older_database(tmp_path):
    path = tmp_path / "old.duckdb"
    with duckdb.connect(str(path)) as old:  # `trains` as first released
        old.execute(
            "CREATE TABLE trains (train_id VARCHAR PRIMARY KEY, number VARCHAR, "
            "label VARCHAR NOT NULL, train_type VARCHAR NOT NULL, line VARCHAR NOT NULL, "
            "direction VARCHAR NOT NULL)"
        )
        old.execute("INSERT INTO trains VALUES ('central-9', '9', 'CSMT', 'slow', 'central', 'up')")
    init_db(path).close()
    with init_db(path) as con:  # applying the schema twice must be harmless
        load_timetable(con, read_timetable(write(tmp_path, ATTRIBUTES)))
        rows = con.execute(ATTRIBUTE_QUERY + " NULLS LAST").fetchall()
    assert rows[0] == ("1", "K 1", True, 15, False)
    assert rows[-1] == ("9", None, None, None, None)


@pytest.mark.parametrize(
    ("text", "mask"),
    [
        ("", "YYYYYYY"),
        ("Daily", "YYYYYYY"),
        ("YYYYYYN", "YYYYYYN"),
        ("nnnnnyy", "NNNNNYY"),
        ("mon-sat", "YYYYYYN"),
        ("sun", "NNNNNNY"),
        ("sat|sun", "NNNNNYY"),
        ("fri-mon", "YNNNYYY"),
        ("mon | wed-thu", "YNYYNNN"),
    ],
)
def test_parse_days(text, mask):
    assert parse_days(text) == mask


@pytest.mark.parametrize("text", ["weekdays", "NNNNNNN", "mon-", "mon,tue", "YYYYYY"])
def test_parse_days_rejects(text):
    with pytest.raises(ValueError):
        parse_days(text)
