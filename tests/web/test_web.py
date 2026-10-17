"""The web app's API (sitt.web.app), against the invented sample timetable. No network."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import SAMPLE_CSV
from fastapi.testclient import TestClient

from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.tz import IST
from sitt.web.app import create_app

NOW = datetime(2026, 9, 28, 7, 0, tzinfo=IST)  # a Monday morning


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    return path


@pytest.fixture
def client(db):
    collected = []

    def fake_collect(db_path, data_dir):
        collected.append((db_path, data_dir))
        return "Live check just now: ntes 3 readings."

    app = create_app(db_path=db, model_dir=None, now=lambda: NOW, collect=fake_collect)
    test_client = TestClient(app)
    test_client.collected = collected
    return test_client


def test_the_page_and_its_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Should I take this train?" in page.text
    for path in (
        "/static/app.js",
        "/static/styles.css",
        "/static/fonts/doto-latin-900-normal.woff2",
    ):
        assert client.get(path).status_code == 200, path
    assert "fonts.googleapis.com" not in page.text  # works offline


def test_status_and_stations(client):
    status = client.get("/api/status").json()
    assert status["database"] and status["trains"] == 13 and status["sample_timetable"]
    assert status["observations"] == 0 and status["last_collected"] is None
    assert status["manual_collection"] is True and status["mobond_enabled"] is False
    stations = client.get("/api/stations").json()
    assert stations[0] == {
        "code": "CSMT",
        "name": "Chhatrapati Shivaji Maharaj Terminus",
        "seq": 0,
    } or (stations[0]["code"] == "CSMT")
    assert {"KYN", "TNA", "DR"} <= {s["code"] for s in stations}


def test_recommend(client):
    response = client.get("/api/recommend", params={"from": "kalyan", "to": "CSMT"})
    assert response.status_code == 200
    answer = response.json()
    assert answer["origin"] == "Kalyan" and answer["origin_code"] == "KYN"
    assert answer["asked_at"] == "2026-09-28T07:00+05:30"
    assert answer["reason"].startswith("Wait for the 07:12 fast: arrives 08:19")
    assert answer["timetable_only"] and answer["level"] == "timetable"
    pick = answer["options"][answer["choice"]]
    assert pick["number"] == "90106" and pick["type"] == "fast"
    assert pick["departs"] == "2026-09-28T07:12+05:30"
    assert pick["crowding"]["score"] in range(1, 6) and pick["live"] is None
    assert "Rules: take the earliest predicted arrival" in answer["explanation"]


def test_recommend_at_a_time_and_for_a_train(client):
    later = client.get(
        "/api/recommend", params={"from": "KYN", "to": "CSMT", "at": "07:30", "date": "2026-09-29"}
    ).json()
    assert later["asked_at"] == "2026-09-29T07:30+05:30"
    assert later["options"][0]["departs"].startswith("2026-09-29T07:31")
    focused = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT", "train": "07:31"})
    assert focused.json()["options"][focused.json()["focus"]]["number"] == "90108"
    missing = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT", "train": "07:13"})
    assert (
        missing.status_code == 404 and "No train leaves Kalyan at 07:13" in missing.json()["error"]
    )


@pytest.mark.parametrize(
    "params, status, text",
    [
        ({"from": "KYN", "to": "Thaane"}, 400, "Did you mean Thane (TNA)?"),
        ({"from": "KYN", "to": "kalyan"}, 400, "two different stations"),
        ({"from": "KYN", "to": "CSMT", "at": "25:00"}, 400, "at must be a time"),
        ({"from": "KYN", "to": "CSMT", "date": "tomorrow"}, 400, "date must look like"),
    ],
)
def test_recommend_errors(client, params, status, text):
    response = client.get("/api/recommend", params=params)
    assert response.status_code == status
    assert text in response.json()["error"]


def test_live_readings_reach_the_page(client, db):
    with init_db(db) as con:
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
            "source, train_number, event) "
            "VALUES (?, 'central-90106', 'KYN', 9, 'ntes', '90106', 'at')",
            [NOW - timedelta(minutes=5)],
        )
    answer = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"}).json()
    fast = next(o for o in answer["options"] if o["number"] == "90106")
    assert fast["live"] == {
        "delay": 9,
        "at": "2026-09-28T06:55+05:30",
        "station": "KYN",
        "source": "ntes",
    }
    assert fast["departure_delay"] == 9 and not answer["timetable_only"]
    assert client.get("/api/status").json()["last_collected"] == "2026-09-28T06:55+05:30"


def test_collect_only_when_asked(client, db):
    client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"})
    assert client.collected == []  # showing trains never collects
    response = client.post("/api/collect")
    assert response.json() == {"message": "Live check just now: ntes 3 readings."}
    assert client.collected == [(db, db.parent)]  # batches beside the database


def test_log_crowding_and_list_reports(client):
    response = client.post(
        "/api/reports",
        json={"station": "Kalyan", "departure": "7:12", "service": "fast", "crowd": 4},
    )
    assert response.status_code == 201
    logged = response.json()
    assert logged["train"] == "07:12 fast from KYN" and logged["crowd_label"] == "packed"
    reports = client.get("/api/reports").json()
    assert reports[0]["id"] == logged["id"] and reports[0]["crowd"] == 4
    # The report feeds the crowding estimate for that train.
    answer = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"}).json()
    fast = next(o for o in answer["options"] if o["number"] == "90106")
    assert "report" in fast["crowding"]["reason"]


def test_patterns_page_survives_a_failing_model_section(client, db, monkeypatch):
    from sitt import patterns

    with init_db(db) as con:
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, "
            "actual_or_expected_time, time_kind, delay_minutes, source, event) VALUES "
            "(?, 'central-90106', 'KYN', ?, 'expected', 8, 'ntes', 'departure')",
            [NOW + timedelta(minutes=17), NOW + timedelta(minutes=20)],
        )

    def broken(con):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(patterns, "model_progress", broken)
    response = client.get("/api/patterns")
    assert response.status_code == 200
    assert response.json()["runs"] == 1 and "model" not in response.json()


def test_a_report_just_logged_can_be_taken_back(client):
    body = {"station": "Kalyan", "departure": "7:12", "service": "fast", "crowd": 5}
    logged = client.post("/api/reports", json=body).json()
    (listed,) = client.get("/api/reports").json()
    assert listed["can_undo"] is True
    assert client.delete(f"/api/reports/{logged['id']}").status_code == 204
    assert client.get("/api/reports").json() == []
    # The estimate no longer uses it.
    answer = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"}).json()
    fast = next(o for o in answer["options"] if o["number"] == "90106")
    assert "report" not in fast["crowding"]["reason"]
    gone = client.delete(f"/api/reports/{logged['id']}")
    assert gone.status_code == 404 and "taken back already" in gone.json()["error"]


def test_older_reports_and_the_bots_cannot_be_taken_back(client, db):
    from sitt.web.app import UNDO_MINUTES

    now = datetime.now(UTC)
    with init_db(db) as con:
        for when, source in [
            (now - timedelta(minutes=UNDO_MINUTES + 1), "web"),  # too old
            (now - timedelta(minutes=1), "telegram"),  # from the bot
        ]:
            con.execute(
                "INSERT INTO crowd_reports (reported_at, station_code, train_description, "
                "crowd_level, source) VALUES (?, 'KYN', '07:12 fast from KYN', 3, ?)",
                [when, source],
            )
    reports = client.get("/api/reports").json()
    assert [r["can_undo"] for r in reports] == [False, False]
    for report in reports:
        refused = client.delete(f"/api/reports/{report['id']}")
        assert refused.status_code == 409
        assert f"in the last {UNDO_MINUTES} minutes" in refused.json()["error"]
    assert len(client.get("/api/reports").json()) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"station": "KYN", "departure": "7:12", "service": "express", "crowd": 4},
        {"station": "KYN", "departure": "7:12", "service": "fast", "crowd": 9},
        {"station": "KYN", "departure": "soon", "service": "fast", "crowd": 3},
        {"station": "Atlantis", "departure": "7:12", "service": "fast", "crowd": 3},
    ],
)
def test_bad_reports_are_refused(client, body):
    assert client.post("/api/reports", json=body).status_code in (400, 422)
    assert client.get("/api/reports").json() == []


def test_without_a_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(db_path=tmp_path / "missing.duckdb", model_dir=None))
    assert client.get("/api/status").json()["database"] is False
    response = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"})
    assert response.status_code == 503 and "sitt-init-db" in response.json()["error"]
    assert not (tmp_path / "missing.duckdb").exists()


# --- found by review ---


def test_reads_and_writes_at_the_same_time_all_succeed(client):
    import concurrent.futures

    def post(i):
        body = {"station": "KYN", "departure": "7:12", "service": "fast", "crowd": 1 + i % 5}
        return client.post("/api/reports", json=body).status_code

    def get(_):
        return client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(post, range(10))) + list(pool.map(get, range(10)))
        mixed = [pool.submit(post if i % 2 else get, i) for i in range(16)]
        codes += [future.result() for future in mixed]
    assert set(codes) <= {200, 201}, codes
    assert len(client.get("/api/reports?limit=50").json()) == 18


def _hold_database(path, ready, release):
    import duckdb

    con = duckdb.connect(str(path))  # a writer in another process, like the bot or collector
    ready.set()
    release.wait(30)
    con.close()


def test_a_database_held_by_another_process_gives_a_clear_busy_answer(client, db):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    holder = context.Process(target=_hold_database, args=(db, ready, release))
    holder.start()
    try:
        assert ready.wait(30)
        response = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"})
        assert response.status_code == 503
        assert "busy" in response.json()["error"]
    finally:
        release.set()
        holder.join(30)
    assert client.get("/api/recommend", params={"from": "KYN", "to": "CSMT"}).status_code == 200


@pytest.mark.parametrize("day", ["9999-12-31", "0001-01-01", "2030-01-01"])
def test_far_away_dates_are_refused_cleanly(client, day):
    response = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT", "date": day})
    assert response.status_code == 400 and "within a year" in response.json()["error"]


def test_validation_errors_are_readable(client):
    response = client.post(
        "/api/reports",
        json={"station": "K" * 80, "departure": "7:12", "service": "fast", "crowd": 3},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error.startswith("station: ") and "60" in error
    bad_n = client.get("/api/recommend", params={"from": "KYN", "to": "CSMT", "n": 99})
    assert bad_n.status_code == 422 and bad_n.json()["error"].startswith("n: ")


def test_patterns_page_and_api(client, db):
    page = client.get("/patterns")
    assert page.status_code == 200 and "How late have trains been?" in page.text
    assert client.get("/static/patterns.js").status_code == 200
    empty = client.get("/api/patterns").json()
    assert empty["runs"] == 0 and empty["by_hour"] == [] and empty["min_runs"] == 5
    assert "model" not in empty  # nothing to say before there are readings

    from sitt.db import init_db

    with init_db(db) as con:
        # 90106 due at Kalyan 07:12, last read close to its time 8 minutes late.
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, "
            "actual_or_expected_time, time_kind, delay_minutes, source, event) VALUES "
            "(?, 'central-90106', 'KYN', ?, 'expected', 8, 'ntes', 'departure')",
            [NOW + timedelta(minutes=17), NOW + timedelta(minutes=20)],
        )
    data = client.get("/api/patterns").json()
    assert data["runs"] == 1 and data["stations"] == ["KYN"]
    (cell,) = data["by_hour"]
    assert (cell["direction"], cell["key"], cell["median"], cell["few"]) == ("up", 7, 8.0, True)
    assert [b["key"] for b in data["bands"]][:2] == ["early", "am_peak"]
    # One day of readings: too few to show weekdays and weekends apart.
    kinds = {k["key"]: k["days"] for k in data["day_kinds"]}
    assert data["split_ready"] is False and data["by_kind"] == {} and sum(kinds.values()) == 1
    # And how far that is from a delay model trained on real readings.
    assert data["model"]["ready"] is False and data["model"]["in_use"] is None
    assert data["model"]["gates"][0] == {
        "name": "days with observations",
        "have": 1,
        "need": 28,
        "met": False,
    }


def test_stations_list_the_main_line_then_the_harbour_line(client, db, tmp_path):
    from conftest import harbour_csv

    with init_db(db) as con:
        load_timetable(con, read_timetable(harbour_csv(tmp_path), line="harbour"))
    listed = client.get("/api/stations").json()
    lines = [s["line"] for s in listed]
    # Main line stations first (CSMT, Masjid, Sandhurst Road and Kurla among them), then
    # the Harbour line's own: the two lines' places never interleave.
    first_harbour = lines.index("harbour")
    assert set(lines[:first_harbour]) == {"central"} and set(lines[first_harbour:]) == {"harbour"}
    main = [s["seq"] for s in listed[:first_harbour]]
    harbour = [s["seq"] for s in listed[first_harbour:]]
    assert main == sorted(main) and harbour == sorted(harbour)
    status = client.get("/api/status").json()
    assert status["lines"] == [
        {"line": "central", "trains": 13},
        {"line": "harbour", "trains": status["trains"] - 13},
    ]
    codes = [s["code"] for s in listed]
    assert codes[0] == "CSMT" and codes[first_harbour] == "DKRD"  # Dockyard Road, 4th
    assert "CLA" in codes[:first_harbour] and codes.count("CSMT") == 1
