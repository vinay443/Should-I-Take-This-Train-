"""The web app's API (sitt.web.app), against the invented sample timetable. No network."""

from datetime import datetime, timedelta

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
