from datetime import date

from sitt.holidays import is_holiday, is_sunday_schedule
from sitt.routes import load_routes, stage_route_points


def test_fast_train_gets_pass_points_at_skipped_stations(loaded):
    routes = {route.number: route for route in load_routes(loaded)}
    fast = routes["90106"]  # KYN 07:12, DI 07:18, TNA 07:33, ... CSMT 08:19
    codes = [p.station_code for p in fast.points]
    assert codes[:4] == ["KYN", "THK", "DI", "KOPR"]
    assert codes[-1] == "CSMT" and len(codes) == 26  # every station from Kalyan to CSMT
    by_code = {p.station_code: p for p in fast.points}
    assert (by_code["KYN"].minutes, by_code["KYN"].is_stop) == (0.0, True)
    assert (by_code["THK"].minutes, by_code["THK"].is_stop) == (3.5, False)  # halfway to DI
    assert (by_code["DI"].minutes, by_code["DI"].is_stop) == (7.0, True)  # departs 07:19
    assert fast.duration == 67.0
    minutes = [p.minutes for p in fast.points]
    assert minutes == sorted(minutes)

    slow = routes["90104"]
    assert all(p.is_stop for p in slow.points)


def test_route_points_table(loaded):
    stage_route_points(loaded)
    rows = loaded.execute(
        "SELECT station_code, point_seq, is_stop, progress FROM route_points "
        "WHERE train_id = 'central-90106' ORDER BY point_seq"
    ).fetchall()
    assert rows[0] == ("KYN", 0, True, 0.0)
    assert rows[-1] == ("CSMT", 25, True, 1.0)
    assert rows[1][:3] == ("THK", 1, False)


def test_sunday_schedule_days():
    assert is_sunday_schedule(date(2026, 10, 4))  # a Sunday
    assert is_sunday_schedule(date(2026, 10, 2))  # Gandhi Jayanti, a Friday
    assert is_holiday(date(2026, 8, 15)) and not is_holiday(date(2026, 8, 14))
    assert not is_sunday_schedule(date(2026, 10, 5))


def test_loading_the_harbour_line_leaves_main_line_routes_alone(loaded, tmp_path):
    from conftest import harbour_csv

    from sitt.ingest.timetable import load_timetable, read_timetable

    before = {r.train_id: r for r in load_routes(loaded)}
    load_timetable(loaded, read_timetable(harbour_csv(tmp_path), line="harbour"))
    after = {r.train_id: r for r in load_routes(loaded)}
    # Dockyard Road is 4th on the Harbour line, Byculla 4th on the main line: a fast
    # train passing Byculla must still pass Byculla, not Dockyard Road.
    assert {k: v for k, v in after.items() if k in before} == before

    harbour = after["harbour-98001"]  # PL 1: CSMT to Panvel, calls everywhere
    assert all(p.is_stop for p in harbour.points)  # nothing added between stops
    codes = [p.station_code for p in harbour.points]
    assert codes[7:9] == ["VDLR", "GTBN"] and codes[-1] == "PNVL"
    seqs = [p.station_seq for p in harbour.points]
    assert seqs == sorted(seqs) and seqs[0] == 1  # Harbour line places, CSMT first
    places = dict(
        loaded.execute("SELECT code, seq FROM station_lines WHERE line = 'harbour'").fetchall()
    )
    assert [places[c] for c in codes] == seqs


def test_route_places_come_from_stations_until_station_lines_has_any():
    import duckdb

    from sitt.db.database import SCHEMA_SQL
    from sitt.routes import line_station_seqs

    with duckdb.connect(":memory:") as con:
        con.execute(SCHEMA_SQL)
        con.execute("INSERT INTO stations VALUES ('CSMT', 'CSMT', 'central', 1)")
        # Written without the loader: station_lines is empty, so stations is used.
        assert line_station_seqs(con) == {"central": {"CSMT": 1}}
        # Once station_lines has rows, it is the whole truth.
        con.execute("INSERT INTO station_lines VALUES ('CSMT', 'harbour', 1)")
        assert line_station_seqs(con) == {"harbour": {"CSMT": 1}}
        # A database made before station_lines existed, not yet opened for writing.
        con.execute("DROP TABLE station_lines")
        assert line_station_seqs(con) == {"central": {"CSMT": 1}}


def test_a_database_from_before_station_lines_still_answers(tmp_path):
    """After a pull, the PC's database has no station_lines until something writes to it;
    sitt-next and the web app open it without applying the schema."""
    from datetime import datetime

    from conftest import SAMPLE_CSV

    from sitt.db import init_db
    from sitt.ingest.timetable import load_timetable, read_timetable
    from sitt.recommend import recommend

    path = tmp_path / "old.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        con.execute("DROP TABLE station_lines")
    import duckdb

    with duckdb.connect(str(path), read_only=True) as con:
        rec = recommend(con, "KYN", "CSMT", datetime(2026, 9, 28, 7, 0), model_dir=None)
    assert rec.chosen.trip.number == "90106"
